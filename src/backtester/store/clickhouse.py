"""ClickHouse BarStore: bars, coverage, instruments, and snapshots."""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import clickhouse_connect
import polars as pl
from clickhouse_connect.driver.client import Client

from backtester.calendar.display import ensure_utc
from backtester.catalog.instruments import instruments_from_row
from backtester.domain import CoverageInterval, Instrument, WriteResult, empty_bars
from backtester.store.interface import BarStore
from backtester.timeframes import Timeframe

BAR_COLUMNS: list[str] = empty_bars().columns
_REQUIRED_BAR_COLUMNS = [c for c in BAR_COLUMNS if c != "adj_close"]

SNAPSHOT_COLUMNS = [
    "source",
    "chain_id",
    "dex_id",
    "pair_address",
    "base_symbol",
    "quote_symbol",
    "base_address",
    "quote_address",
    "price_usd",
    "price_native",
    "liquidity_usd",
    "volume_m5",
    "volume_h1",
    "volume_h6",
    "volume_h24",
    "price_change_h24",
    "fdv",
    "market_cap",
    "pair_created_at",
    "polled_at_utc",
    "quality",
    "url",
]

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS {db}.bars
(
    instrument_id String,
    source String,
    timeframe String,
    ts_utc DateTime64(6, 'UTC'),
    open Float64,
    high Float64,
    low Float64,
    close Float64,
    volume Float64,
    is_partial Bool,
    quality String,
    adj_close Nullable(Float64)
)
ENGINE = ReplacingMergeTree
PARTITION BY (timeframe, toYYYYMM(ts_utc))
ORDER BY (instrument_id, timeframe, source, ts_utc);

CREATE TABLE IF NOT EXISTS {db}.coverage
(
    instrument_id String,
    timeframe String,
    source String,
    start_utc DateTime64(6, 'UTC'),
    end_utc DateTime64(6, 'UTC')
)
ENGINE = MergeTree
ORDER BY (instrument_id, timeframe, source, start_utc);

CREATE TABLE IF NOT EXISTS {db}.instruments
(
    instrument_id String,
    asset_class String,
    venue String,
    symbol String,
    session_tz String,
    aliases String,
    alternatives String,
    pair_created_at Nullable(DateTime64(6, 'UTC')),
    base_token Nullable(String),
    quote_token Nullable(String)
)
ENGINE = ReplacingMergeTree
ORDER BY instrument_id;

CREATE TABLE IF NOT EXISTS {db}.aliases
(
    alias_key String,
    instrument_id String
)
ENGINE = ReplacingMergeTree
ORDER BY alias_key;

CREATE TABLE IF NOT EXISTS {db}.snapshots
(
    source String,
    chain_id String,
    dex_id String,
    pair_address String,
    base_symbol String,
    quote_symbol String,
    base_address Nullable(String),
    quote_address Nullable(String),
    price_usd Nullable(Float64),
    price_native Nullable(Float64),
    liquidity_usd Nullable(Float64),
    volume_m5 Nullable(Float64),
    volume_h1 Nullable(Float64),
    volume_h6 Nullable(Float64),
    volume_h24 Nullable(Float64),
    price_change_h24 Nullable(Float64),
    fdv Nullable(Float64),
    market_cap Nullable(Float64),
    pair_created_at Nullable(DateTime64(6, 'UTC')),
    polled_at_utc DateTime64(6, 'UTC'),
    quality String,
    url Nullable(String)
)
ENGINE = ReplacingMergeTree
PARTITION BY toYYYYMM(polled_at_utc)
ORDER BY (source, chain_id, pair_address, polled_at_utc);

CREATE TABLE IF NOT EXISTS {db}.snapshot_polls
(
    instrument_id String,
    source String,
    polled_at_utc DateTime64(6, 'UTC')
)
ENGINE = ReplacingMergeTree
ORDER BY (instrument_id, source);
"""


class ClickHouseStore(BarStore):
    """BarStore backed by self-hosted ClickHouse."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8123,
        username: str = "default",
        password: str = "",
        database: str = "backtester",
        client: Client | None = None,
    ) -> None:
        self.database = database
        self._lock = threading.RLock()
        if client is not None:
            self._client = client
        else:
            # Connect without a default DB so the first open can create it.
            bootstrap = clickhouse_connect.get_client(
                host=host,
                port=port,
                username=username,
                password=password,
            )
            bootstrap.command(f"CREATE DATABASE IF NOT EXISTS {database}")
            bootstrap.close()
            self._client = clickhouse_connect.get_client(
                host=host,
                port=port,
                username=username,
                password=password,
                database=database,
            )
        self._ensure_schema()

    def close(self) -> None:
        """Close the HTTP client."""
        with self._lock:
            self._client.close()

    def write_batch(self, bars: pl.DataFrame) -> WriteResult:
        """Insert bars; ReplacingMergeTree keeps last write for the ORDER BY key."""
        if bars.is_empty():
            return WriteResult(rows_written=0)
        frame = _prepare_bars(bars).select(BAR_COLUMNS)
        self._insert_frame(f"{self.database}.bars", frame, BAR_COLUMNS)
        return WriteResult(rows_written=frame.height, partitions=1, files=())

    def write_snapshots(self, snapshots: pl.DataFrame) -> WriteResult:
        """Persist PairSnapshots and bump snapshot_polls."""
        if snapshots.is_empty():
            return WriteResult(rows_written=0)
        cols = [c for c in SNAPSHOT_COLUMNS if c in snapshots.columns]
        frame = snapshots.select(cols)
        for col in ("polled_at_utc", "pair_created_at"):
            if col in frame.columns:
                frame = _utc_col(frame, col)
        self._insert_frame(f"{self.database}.snapshots", frame, cols)
        if {"chain_id", "pair_address", "source", "polled_at_utc"} <= set(frame.columns):
            for row in frame.select(["source", "chain_id", "pair_address", "polled_at_utc"]).iter_rows(named=True):
                iid = f"DEX:{row['chain_id']}:{row['pair_address']}"
                self._upsert_snapshot_poll(iid, str(row["source"]), ensure_utc(row["polled_at_utc"]))
        return WriteResult(rows_written=frame.height, partitions=1, files=())

    def scan(
        self,
        instrument_id: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        *,
        columns: Sequence[str] | None = None,
        source: str | None = None,
    ) -> pl.LazyFrame:
        """Return matching bars as a LazyFrame (materialized from ClickHouse)."""
        start_utc = ensure_utc(start)
        end_utc = ensure_utc(end)
        select_cols = list(BAR_COLUMNS)
        if columns:
            select_cols = list(dict.fromkeys(["ts_utc", *columns]))
            select_cols = [c for c in select_cols if c in BAR_COLUMNS]
        col_sql = ", ".join(select_cols)
        where = [
            "instrument_id = {iid:String}",
            "timeframe = {tf:String}",
            "ts_utc >= {start:DateTime64(6)}",
            "ts_utc <= {end:DateTime64(6)}",
        ]
        params: dict[str, Any] = {
            "iid": instrument_id,
            "tf": timeframe,
            "start": start_utc,
            "end": end_utc,
        }
        if source is not None:
            where.append("source = {src:String}")
            params["src"] = source
        sql = f"SELECT {col_sql} FROM {self.database}.bars FINAL WHERE {' AND '.join(where)} ORDER BY ts_utc"
        with self._lock:
            result = self._client.query(sql, parameters=params)
        if not result.result_rows:
            lf = empty_bars().lazy()
            if columns:
                ordered = list(dict.fromkeys(["ts_utc", *columns]))
                keep = [c for c in ordered if c in empty_bars().columns]
                return lf.select(keep)
            return lf
        frame = pl.DataFrame(result.result_rows, schema=result.column_names, orient="row")
        if "ts_utc" in frame.columns:
            frame = frame.with_columns(pl.col("ts_utc").cast(pl.Datetime("us", "UTC")))
        frame = _ensure_adj_close(frame)
        return frame.lazy()

    def coverage(
        self,
        instrument_id: str,
        timeframe: Timeframe,
        source: str,
    ) -> list[CoverageInterval]:
        """Load Coverage rows for one triple."""
        sql = (
            f"SELECT instrument_id, timeframe, source, start_utc, end_utc "
            f"FROM {self.database}.coverage "
            f"WHERE instrument_id = {{iid:String}} AND timeframe = {{tf:String}} "
            f"AND source = {{src:String}} ORDER BY start_utc"
        )
        with self._lock:
            rows = self._client.query(
                sql, parameters={"iid": instrument_id, "tf": timeframe, "src": source}
            ).result_rows
        return [_row_to_coverage(r) for r in rows]

    def list_coverage(self) -> list[CoverageInterval]:
        """Load every Coverage row."""
        sql = (
            f"SELECT instrument_id, timeframe, source, start_utc, end_utc "
            f"FROM {self.database}.coverage "
            f"ORDER BY instrument_id, timeframe, source, start_utc"
        )
        with self._lock:
            rows = self._client.query(sql).result_rows
        return [_row_to_coverage(r) for r in rows]

    def update_coverage(
        self,
        instrument_id: str,
        timeframe: Timeframe,
        source: str,
        start: datetime,
        end: datetime,
    ) -> None:
        """Union an inclusive UTC range into Coverage, merging overlaps."""
        start_utc = ensure_utc(start)
        end_utc = ensure_utc(end)
        existing = self.coverage(instrument_id, timeframe, source)
        merged = _merge_coverage(existing, start_utc, end_utc, instrument_id, timeframe, source)
        self._replace_coverage(instrument_id, timeframe, source, merged)

    def drop_coverage(
        self,
        instrument_id: str,
        timeframe: Timeframe,
        source: str,
        start: datetime,
        end: datetime,
    ) -> None:
        """Subtract ``[start, end]`` from Coverage (inclusive)."""
        start_utc = ensure_utc(start)
        end_utc = ensure_utc(end)
        existing = self.coverage(instrument_id, timeframe, source)
        kept: list[CoverageInterval] = []
        for item in existing:
            if item.end_utc < start_utc or item.start_utc > end_utc:
                kept.append(item)
                continue
            if item.start_utc < start_utc:
                kept.append(item.model_copy(update={"end_utc": start_utc - timedelta(microseconds=1)}))
            if item.end_utc > end_utc:
                kept.append(item.model_copy(update={"start_utc": end_utc + timedelta(microseconds=1)}))
        self._replace_coverage(
            instrument_id,
            timeframe,
            source,
            [item for item in kept if item.end_utc >= item.start_utc],
        )

    def compact(self, instrument_id: str | None = None) -> int:
        """No-op: ClickHouse merges parts itself. Returns 0 files removed."""
        return 0

    def prune_hot_1m(self, *, hot_days: int, now: datetime | None = None) -> int:
        """Delete 1m bars older than ``hot_days`` when 1h or 1d coverage exists."""
        now_utc = ensure_utc(now or datetime.now(UTC))
        cutoff = now_utc - timedelta(days=hot_days)
        sql = (
            f"SELECT instrument_id, source, toYYYYMM(ts_utc) AS ym, count() AS n "
            f"FROM {self.database}.bars FINAL "
            f"WHERE timeframe = '1m' AND ts_utc < {{cutoff:DateTime64(6)}} "
            f"GROUP BY instrument_id, source, ym"
        )
        with self._lock:
            groups = self._client.query(sql, parameters={"cutoff": cutoff}).result_rows
        removed = 0
        for instrument_id, source, ym, n in groups:
            iid = str(instrument_id)
            src = str(source)
            if not self._has_coarse(iid, src):
                continue
            year = int(ym) // 100
            month = int(ym) % 100
            month_start = datetime(year, month, 1, tzinfo=UTC)
            month_end = _month_end_utc(year, month)
            delete_sql = (
                f"ALTER TABLE {self.database}.bars DELETE WHERE "
                f"instrument_id = {{iid:String}} AND source = {{src:String}} "
                f"AND timeframe = '1m' AND ts_utc >= {{start:DateTime64(6)}} "
                f"AND ts_utc <= {{end:DateTime64(6)}} "
                f"SETTINGS mutations_sync = 1"
            )
            with self._lock:
                self._client.command(
                    delete_sql,
                    parameters={
                        "iid": iid,
                        "src": src,
                        "start": month_start,
                        "end": month_end,
                    },
                )
            self.drop_coverage(iid, "1m", src, month_start, month_end)
            removed += int(n)
        return removed

    def upsert_instrument(self, instrument: Instrument) -> None:
        """Write Catalog record + alias rows."""
        aliases = json.dumps(list(instrument.aliases))
        alts = json.dumps(list(instrument.alternatives))
        with self._lock:
            self._client.command(
                f"ALTER TABLE {self.database}.instruments DELETE WHERE "
                f"instrument_id = {{iid:String}} SETTINGS mutations_sync = 1",
                parameters={"iid": instrument.instrument_id},
            )
            self._client.command(
                f"ALTER TABLE {self.database}.aliases DELETE WHERE "
                f"instrument_id = {{iid:String}} SETTINGS mutations_sync = 1",
                parameters={"iid": instrument.instrument_id},
            )
            self._client.insert(
                f"{self.database}.instruments",
                [
                    [
                        instrument.instrument_id,
                        instrument.asset_class,
                        instrument.venue,
                        instrument.symbol,
                        instrument.session_tz,
                        aliases,
                        alts,
                        instrument.pair_created_at,
                        instrument.base_token,
                        instrument.quote_token,
                    ]
                ],
                column_names=[
                    "instrument_id",
                    "asset_class",
                    "venue",
                    "symbol",
                    "session_tz",
                    "aliases",
                    "alternatives",
                    "pair_created_at",
                    "base_token",
                    "quote_token",
                ],
            )
            alias_rows = [
                [alias.strip().lower(), instrument.instrument_id]
                for alias in {instrument.instrument_id, instrument.symbol, *instrument.aliases}
            ]
            if alias_rows:
                self._client.insert(
                    f"{self.database}.aliases",
                    alias_rows,
                    column_names=["alias_key", "instrument_id"],
                )

    def get_instrument(self, instrument_id: str) -> Instrument | None:
        """Load one Instrument."""
        sql = (
            f"SELECT instrument_id, asset_class, venue, symbol, session_tz, "
            f"aliases, alternatives, pair_created_at, base_token, quote_token "
            f"FROM {self.database}.instruments FINAL "
            f"WHERE instrument_id = {{iid:String}} LIMIT 1"
        )
        with self._lock:
            rows = self._client.query(sql, parameters={"iid": instrument_id}).result_rows
        if not rows:
            return None
        cols = [
            "instrument_id",
            "asset_class",
            "venue",
            "symbol",
            "session_tz",
            "aliases",
            "alternatives",
            "pair_created_at",
            "base_token",
            "quote_token",
        ]
        return instruments_from_row(dict(zip(cols, rows[0], strict=True)))

    def find_by_alias(self, alias: str) -> Instrument | None:
        """Case-insensitive alias lookup."""
        sql = f"SELECT instrument_id FROM {self.database}.aliases FINAL WHERE alias_key = {{key:String}} LIMIT 1"
        with self._lock:
            rows = self._client.query(sql, parameters={"key": alias.strip().lower()}).result_rows
        if not rows:
            return None
        return self.get_instrument(str(rows[0][0]))

    def last_snapshot_poll(self, instrument_id: str, source: str) -> datetime | None:
        """Last PairSnapshot poll time for a DEX instrument."""
        sql = (
            f"SELECT polled_at_utc FROM {self.database}.snapshot_polls FINAL "
            f"WHERE instrument_id = {{iid:String}} AND source = {{src:String}} LIMIT 1"
        )
        with self._lock:
            rows = self._client.query(sql, parameters={"iid": instrument_id, "src": source}).result_rows
        if not rows or rows[0][0] is None:
            return None
        return ensure_utc(rows[0][0])

    def truncate_all(self) -> None:
        """Empty every store table. Used by tests."""
        with self._lock:
            for table in ("bars", "coverage", "instruments", "aliases", "snapshots", "snapshot_polls"):
                self._client.command(f"TRUNCATE TABLE IF EXISTS {self.database}.{table}")

    def _ensure_schema(self) -> None:
        with self._lock:
            self._client.command(f"CREATE DATABASE IF NOT EXISTS {self.database}")
            for stmt in _SCHEMA_SQL.format(db=self.database).split(";"):
                sql = stmt.strip()
                if sql:
                    self._client.command(sql)

    def _insert_frame(self, table: str, frame: pl.DataFrame, columns: Sequence[str]) -> None:
        data = []
        for row in frame.select(list(columns)).iter_rows():
            data.append(list(row))
        if not data:
            return
        with self._lock:
            self._client.insert(table, data, column_names=list(columns))

    def _replace_coverage(
        self,
        instrument_id: str,
        timeframe: Timeframe,
        source: str,
        rows: list[CoverageInterval],
    ) -> None:
        with self._lock:
            self._client.command(
                f"ALTER TABLE {self.database}.coverage DELETE WHERE "
                f"instrument_id = {{iid:String}} AND timeframe = {{tf:String}} "
                f"AND source = {{src:String}} SETTINGS mutations_sync = 1",
                parameters={"iid": instrument_id, "tf": timeframe, "src": source},
            )
            if rows:
                self._client.insert(
                    f"{self.database}.coverage",
                    [[r.instrument_id, r.timeframe, r.source, r.start_utc, r.end_utc] for r in rows],
                    column_names=[
                        "instrument_id",
                        "timeframe",
                        "source",
                        "start_utc",
                        "end_utc",
                    ],
                )

    def _has_coarse(self, instrument_id: str, source: str) -> bool:
        return any(self.coverage(instrument_id, tf, source) for tf in ("1h", "1d"))

    def _upsert_snapshot_poll(self, instrument_id: str, source: str, polled: datetime) -> None:
        with self._lock:
            self._client.command(
                f"ALTER TABLE {self.database}.snapshot_polls DELETE WHERE "
                f"instrument_id = {{iid:String}} AND source = {{src:String}} "
                f"SETTINGS mutations_sync = 1",
                parameters={"iid": instrument_id, "src": source},
            )
            self._client.insert(
                f"{self.database}.snapshot_polls",
                [[instrument_id, source, polled]],
                column_names=["instrument_id", "source", "polled_at_utc"],
            )


def _row_to_coverage(row: Sequence[Any]) -> CoverageInterval:
    return CoverageInterval(
        instrument_id=str(row[0]),
        timeframe=str(row[1]),  # type: ignore[arg-type]
        source=str(row[2]),
        start_utc=ensure_utc(row[3]),
        end_utc=ensure_utc(row[4]),
    )


def _prepare_bars(bars: pl.DataFrame) -> pl.DataFrame:
    frame = bars
    missing = [c for c in _REQUIRED_BAR_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"write_batch missing columns: {missing}")
    frame = _ensure_adj_close(frame)
    frame = _utc_col(frame, "ts_utc")
    return _dedup_bars(frame)


def _utc_col(frame: pl.DataFrame, column: str) -> pl.DataFrame:
    dtype = frame.schema.get(column)
    if dtype is None:
        return frame
    if dtype == pl.Datetime("us", "UTC"):
        return frame
    return frame.with_columns(pl.col(column).dt.convert_time_zone("UTC").alias(column))


def _ensure_adj_close(frame: pl.DataFrame) -> pl.DataFrame:
    if "adj_close" not in frame.columns:
        return frame.with_columns(pl.col("close").alias("adj_close"))
    return frame.with_columns(pl.col("adj_close").fill_null(pl.col("close")))


def _dedup_bars(frame: pl.DataFrame) -> pl.DataFrame:
    if "ts_utc" not in frame.columns:
        return frame
    if frame.schema["ts_utc"] != pl.Datetime("us", "UTC"):
        frame = _utc_col(frame, "ts_utc")
    return frame.sort(["instrument_id", "timeframe", "source", "ts_utc"]).unique(
        subset=["instrument_id", "timeframe", "ts_utc"],
        keep="last",
        maintain_order=True,
    )


def _merge_coverage(
    existing: list[CoverageInterval],
    start: datetime,
    end: datetime,
    instrument_id: str,
    timeframe: Timeframe,
    source: str,
) -> list[CoverageInterval]:
    items = [
        *existing,
        CoverageInterval(
            instrument_id=instrument_id,
            timeframe=timeframe,
            source=source,
            start_utc=start,
            end_utc=end,
        ),
    ]
    items.sort(key=lambda c: c.start_utc)
    merged: list[CoverageInterval] = [items[0]]
    for current in items[1:]:
        last = merged[-1]
        if current.start_utc <= last.end_utc + timedelta(microseconds=1):
            merged[-1] = last.model_copy(update={"end_utc": max(last.end_utc, current.end_utc)})
        else:
            merged.append(current)
    return merged


def _month_end_utc(year: int, month: int) -> datetime:
    if month == 12:
        return datetime(year + 1, 1, 1, tzinfo=UTC) - timedelta(microseconds=1)
    return datetime(year, month + 1, 1, tzinfo=UTC) - timedelta(microseconds=1)
