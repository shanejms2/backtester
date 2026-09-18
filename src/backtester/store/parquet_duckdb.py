"""Parquet (Hive, ZSTD) + DuckDB catalog. The v1 BarStore adapter."""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import polars as pl

from backtester.calendar.display import ensure_utc
from backtester.catalog.instruments import (
    hive_symbol,
    instruments_from_row,
    parse_instrument_id,
)
from backtester.domain import CoverageInterval, Instrument, WriteResult
from backtester.store.interface import BarStore
from backtester.timeframes import Timeframe

BAR_COLUMNS = [
    "instrument_id",
    "source",
    "timeframe",
    "ts_utc",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "is_partial",
    "quality",
]

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

_WRITE_ROWS = 250_000
_COMPACT_MIN_FILES = 8
_COMPACT_AVG_BYTES = 1_000_000


def empty_bars() -> pl.DataFrame:
    """Empty Bars frame with the canonical schema."""
    return pl.DataFrame(
        schema={
            "instrument_id": pl.Utf8,
            "source": pl.Utf8,
            "timeframe": pl.Utf8,
            "ts_utc": pl.Datetime("us", "UTC"),
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Float64,
            "is_partial": pl.Boolean,
            "quality": pl.Utf8,
        }
    )


def _utc_series(frame: pl.DataFrame, column: str) -> pl.DataFrame:
    dtype = frame.schema.get(column)
    if dtype is None:
        return frame
    if dtype == pl.Datetime("us", "UTC"):
        return frame
    return frame.with_columns(pl.col(column).dt.convert_time_zone("UTC").alias(column))


class _PartitionLock:
    """Exclusive flock per Hive partition so two Requests cannot clobber a write."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    def __enter__(self) -> _PartitionLock:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o644)
        import fcntl

        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *args: object) -> None:
        if self._fd is not None:
            import fcntl

            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


class ParquetDuckDBStore(BarStore):
    """Hive-partitioned Parquet bars plus a DuckDB metadata file.

    Ingest talks only to ``BarStore``. Partition paths stay inside this adapter.
    """

    def __init__(self, root: Path) -> None:
        """Open (or create) the catalog under ``root``.

        Args:
            root: Data directory (typically ``data/``). Created if missing.
        """
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._db_path = self.root / "catalog.duckdb"
        self._duck_lock = threading.RLock()
        self._conn = duckdb.connect(str(self._db_path))
        self._init_catalog()

    def close(self) -> None:
        """Close the DuckDB connection."""
        with self._duck_lock:
            self._conn.close()

    def write_batch(self, bars: pl.DataFrame) -> WriteResult:
        """Dedup on ``(instrument_id, timeframe, ts_utc)`` last-write-wins per source."""
        if bars.is_empty():
            return WriteResult(rows_written=0)
        frame = _prepare_bars(bars)
        written = 0
        files: list[str] = []
        partitions = 0
        for keys, part in frame.group_by(
            ["instrument_id", "source", "timeframe", "year", "month"],
            maintain_order=True,
        ):
            payload = part.select(BAR_COLUMNS)
            path = self._write_bar_partition(tuple(keys), payload)
            written += payload.height
            files.append(path)
            partitions += 1
        return WriteResult(rows_written=written, partitions=partitions, files=tuple(files))

    def write_snapshots(self, snapshots: pl.DataFrame) -> WriteResult:
        """Write PairSnapshots under ``snapshots/`` and record last poll in Coverage-like metadata."""
        if snapshots.is_empty():
            return WriteResult(rows_written=0)
        frame = snapshots
        if "year" not in frame.columns:
            frame = frame.with_columns(
                pl.col("polled_at_utc").dt.year().alias("year"),
                pl.col("polled_at_utc").dt.month().alias("month"),
            )
        written = 0
        files: list[str] = []
        for keys, part in frame.group_by(
            ["source", "chain_id", "pair_address", "year", "month"],
            maintain_order=True,
        ):
            source, chain, pair, year, month = keys
            folder = (
                self.root
                / "snapshots"
                / f"source={source}"
                / f"chain={chain}"
                / f"pair={pair}"
                / f"year={int(year):04d}"
                / f"month={int(month):02d}"
            )
            payload = part.select([c for c in SNAPSHOT_COLUMNS if c in part.columns])
            path = self._atomic_write(folder, payload)
            self._record_file(path, payload, "polled_at_utc", kind="snapshots")
            written += payload.height
            files.append(path)
            max_ts = payload.select(pl.col("polled_at_utc").max()).item()
            self._upsert_snapshot_poll(str(chain), str(pair), str(source), ensure_utc(max_ts))
        return WriteResult(rows_written=written, partitions=len(files), files=tuple(files))

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
        """Scan only year/month folders overlapping ``[start, end]``."""
        start_utc = ensure_utc(start)
        end_utc = ensure_utc(end)
        paths = self._bar_paths(instrument_id, timeframe, start_utc, end_utc)
        schema = empty_bars().schema
        if not paths:
            lf = empty_bars().lazy()
        else:
            lf = pl.scan_parquet(paths, hive_partitioning=True)
            available = set(BAR_COLUMNS)
            lf = lf.select([c for c in BAR_COLUMNS if c in available])
        lf = lf.filter(
            (pl.col("instrument_id") == instrument_id)
            & (pl.col("timeframe") == timeframe)
            & (pl.col("ts_utc") >= start_utc)
            & (pl.col("ts_utc") <= end_utc)
        )
        if source is not None:
            lf = lf.filter(pl.col("source") == source)
        if columns:
            keep = ["ts_utc", *columns]
            # Always keep ts_utc; drop duplicates while preserving order.
            ordered = list(dict.fromkeys(keep))
            lf = lf.select([c for c in ordered if c in schema or c == "ts_utc"])
        return lf

    def coverage(
        self,
        instrument_id: str,
        timeframe: Timeframe,
        source: str,
    ) -> list[CoverageInterval]:
        """Load Coverage rows for one triple."""
        with self._duck_lock:
            rows = self._conn.execute(
                """
                SELECT instrument_id, timeframe, source, start_utc, end_utc
                FROM coverage
                WHERE instrument_id = ? AND timeframe = ? AND source = ?
                ORDER BY start_utc
                """,
                [instrument_id, timeframe, source],
            ).fetchall()
        return [
            CoverageInterval(
                instrument_id=str(r[0]),
                timeframe=str(r[1]),  # type: ignore[arg-type]
                source=str(r[2]),
                start_utc=ensure_utc(r[3]),
                end_utc=ensure_utc(r[4]),
            )
            for r in rows
        ]

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
        with self._duck_lock:
            self._conn.execute(
                "DELETE FROM coverage WHERE instrument_id = ? AND timeframe = ? AND source = ?",
                [instrument_id, timeframe, source],
            )
            for item in merged:
                self._conn.execute(
                    """
                    INSERT INTO coverage (instrument_id, timeframe, source, start_utc, end_utc)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [item.instrument_id, item.timeframe, item.source, item.start_utc, item.end_utc],
                )
            self._conn.commit()

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
        with self._duck_lock:
            self._conn.execute(
                "DELETE FROM coverage WHERE instrument_id = ? AND timeframe = ? AND source = ?",
                [instrument_id, timeframe, source],
            )
            for item in kept:
                if item.end_utc >= item.start_utc:
                    self._conn.execute(
                        """
                        INSERT INTO coverage (instrument_id, timeframe, source, start_utc, end_utc)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        [item.instrument_id, item.timeframe, item.source, item.start_utc, item.end_utc],
                    )
            self._conn.commit()

    def compact(self, instrument_id: str | None = None) -> int:
        """Merge tiny Parquet parts when a partition has too many / too-small files."""
        removed = 0
        bars_root = self.root / "bars"
        if not bars_root.exists():
            return 0
        for folder in _partition_dirs(bars_root):
            if instrument_id is not None and instrument_id not in str(folder):
                # Folder uses hive symbol, not full id; skip only when symbol missing.
                _, _, symbol = parse_instrument_id(instrument_id)
                hive = symbol.replace("/", "-").replace(":", "-")
                if f"symbol={hive}" not in str(folder):
                    continue
            parts = sorted(folder.glob("part-*.parquet"))
            if len(parts) < 2:
                continue
            sizes = [p.stat().st_size for p in parts]
            avg = sum(sizes) / len(sizes)
            if len(parts) < _COMPACT_MIN_FILES and avg >= _COMPACT_AVG_BYTES:
                continue
            combined = pl.concat([pl.read_parquet(p) for p in parts], how="diagonal_relaxed")
            combined = _dedup_bars(combined)
            with _PartitionLock(folder / ".lock"):
                new_path = self._atomic_write(folder, combined.select(BAR_COLUMNS), replace_parts=parts)
            for old in parts:
                self._delete_file_row(str(old))
            self._record_file(new_path, combined, "ts_utc", kind="bars")
            removed += len(parts)
        return removed

    def prune_hot_1m(self, *, hot_days: int, now: datetime | None = None) -> int:
        """Drop 1m files older than ``hot_days`` when 1h or 1d Coverage exists for the same source."""
        now_utc = ensure_utc(now or datetime.now(UTC))
        cutoff = now_utc - timedelta(days=hot_days)
        removed = 0
        bars_root = self.root / "bars"
        if not bars_root.exists():
            return 0
        for folder in _partition_dirs(bars_root):
            if "/timeframe=1m/" not in str(folder).replace("\\", "/"):
                continue
            parts = list(folder.glob("part-*.parquet"))
            if not parts:
                continue
            sample = pl.read_parquet(parts[0], n_rows=1)
            if sample.is_empty() or "instrument_id" not in sample.columns:
                continue
            instrument_id = str(sample["instrument_id"][0])
            source = str(sample["source"][0]) if "source" in sample.columns else ""
            if not self._has_coarse(instrument_id, source):
                continue
            # year=/month= from folder
            year, month = _year_month(folder)
            month_end = _month_end_utc(year, month)
            if month_end >= cutoff:
                continue
            with _PartitionLock(folder / ".lock"):
                for part in parts:
                    part.unlink(missing_ok=True)
                    self._delete_file_row(str(part))
                    removed += 1
            self.drop_coverage(
                instrument_id,
                "1m",
                source,
                datetime(year, month, 1, tzinfo=UTC),
                month_end,
            )
        return removed

    def upsert_instrument(self, instrument: Instrument) -> None:
        """Write Catalog record + alias rows."""
        aliases = json.dumps(list(instrument.aliases))
        alts = json.dumps(list(instrument.alternatives))
        created = instrument.pair_created_at
        with self._duck_lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO instruments (
                    instrument_id, asset_class, venue, symbol, session_tz,
                    aliases, alternatives, pair_created_at, base_token, quote_token
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    instrument.instrument_id,
                    instrument.asset_class,
                    instrument.venue,
                    instrument.symbol,
                    instrument.session_tz,
                    aliases,
                    alts,
                    created,
                    instrument.base_token,
                    instrument.quote_token,
                ],
            )
            self._conn.execute("DELETE FROM aliases WHERE instrument_id = ?", [instrument.instrument_id])
            for alias in {instrument.instrument_id, instrument.symbol, *instrument.aliases}:
                self._conn.execute(
                    "INSERT OR REPLACE INTO aliases (alias_key, instrument_id) VALUES (?, ?)",
                    [alias.strip().lower(), instrument.instrument_id],
                )
            self._conn.commit()

    def get_instrument(self, instrument_id: str) -> Instrument | None:
        """Load one Instrument."""
        with self._duck_lock:
            result = self._conn.execute(
                "SELECT * FROM instruments WHERE instrument_id = ?",
                [instrument_id],
            )
            cols = [d[0] for d in result.description]
            row = result.fetchone()
        if row is None:
            return None
        return instruments_from_row(dict(zip(cols, row, strict=True)))

    def find_by_alias(self, alias: str) -> Instrument | None:
        """Case-insensitive alias lookup."""
        with self._duck_lock:
            row = self._conn.execute(
                "SELECT instrument_id FROM aliases WHERE alias_key = ?",
                [alias.strip().lower()],
            ).fetchone()
        if row is None:
            return None
        return self.get_instrument(str(row[0]))

    def last_snapshot_poll(self, instrument_id: str, source: str) -> datetime | None:
        """Last PairSnapshot poll time for a DEX instrument."""
        with self._duck_lock:
            row = self._conn.execute(
                """
                SELECT polled_at_utc FROM snapshot_polls
                WHERE instrument_id = ? AND source = ?
                """,
                [instrument_id, source],
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return ensure_utc(row[0])

    def _init_catalog(self) -> None:
        with self._duck_lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS instruments (
                    instrument_id VARCHAR PRIMARY KEY,
                    asset_class VARCHAR,
                    venue VARCHAR,
                    symbol VARCHAR,
                    session_tz VARCHAR,
                    aliases VARCHAR,
                    alternatives VARCHAR,
                    pair_created_at TIMESTAMPTZ,
                    base_token VARCHAR,
                    quote_token VARCHAR
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS aliases (
                    alias_key VARCHAR PRIMARY KEY,
                    instrument_id VARCHAR
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS coverage (
                    instrument_id VARCHAR,
                    timeframe VARCHAR,
                    source VARCHAR,
                    start_utc TIMESTAMPTZ,
                    end_utc TIMESTAMPTZ
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS files (
                    path VARCHAR PRIMARY KEY,
                    bytes BIGINT,
                    row_count BIGINT,
                    min_ts TIMESTAMPTZ,
                    max_ts TIMESTAMPTZ,
                    kind VARCHAR
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS snapshot_polls (
                    instrument_id VARCHAR,
                    source VARCHAR,
                    polled_at_utc TIMESTAMPTZ,
                    PRIMARY KEY (instrument_id, source)
                )
                """
            )
            self._conn.commit()

    def _bar_folder(
        self,
        instrument: Instrument,
        timeframe: str,
        year: int,
        month: int,
    ) -> Path:
        return (
            self.root
            / "bars"
            / f"asset_class={instrument.asset_class}"
            / f"venue={instrument.venue}"
            / f"symbol={hive_symbol(instrument)}"
            / f"timeframe={timeframe}"
            / f"year={year:04d}"
            / f"month={month:02d}"
        )

    def _write_bar_partition(self, keys: tuple[object, ...], payload: pl.DataFrame) -> str:
        instrument_id, source, timeframe, year, month = keys
        inst = self.get_instrument(str(instrument_id))
        if inst is None:
            asset_class, venue, symbol = parse_instrument_id(str(instrument_id))
            inst = Instrument(
                instrument_id=str(instrument_id),
                asset_class=asset_class,
                venue=venue,
                symbol=symbol,
                session_tz="America/New_York" if asset_class == "equity" else "UTC",
            )
        folder = self._bar_folder(inst, str(timeframe), int(str(year)), int(str(month)))
        with _PartitionLock(folder / ".lock"):
            existing_files = list(folder.glob("part-*.parquet"))
            if existing_files:
                existing = pl.concat(
                    [pl.read_parquet(p) for p in existing_files],
                    how="diagonal_relaxed",
                )
                combined = _dedup_bars(pl.concat([existing, payload], how="diagonal_relaxed"))
                path = self._atomic_write(folder, combined.select(BAR_COLUMNS), replace_parts=existing_files)
                for old in existing_files:
                    self._delete_file_row(str(old))
            else:
                if payload.height > _WRITE_ROWS:
                    path = self._write_chunked(folder, payload)
                else:
                    path = self._atomic_write(folder, payload)
            self._record_file(path, payload, "ts_utc", kind="bars")
        return path

    def _write_chunked(self, folder: Path, payload: pl.DataFrame) -> str:
        last = ""
        for start in range(0, payload.height, _WRITE_ROWS):
            chunk = payload.slice(start, _WRITE_ROWS)
            last = self._atomic_write(folder, chunk)
            self._record_file(last, chunk, "ts_utc", kind="bars")
        return last

    def _atomic_write(
        self,
        folder: Path,
        payload: pl.DataFrame,
        replace_parts: Sequence[Path] | None = None,
    ) -> str:
        folder.mkdir(parents=True, exist_ok=True)
        tmp_dir = folder / ".tmp"
        tmp_dir.mkdir(exist_ok=True)
        name = f"part-{uuid.uuid4().hex[:12]}.parquet"
        tmp = tmp_dir / name
        payload.write_parquet(tmp, compression="zstd", statistics=True)
        written = pl.read_parquet(tmp)
        if written.height != payload.height:
            tmp.unlink(missing_ok=True)
            raise OSError(f"parquet row count mismatch: {written.height} != {payload.height}")
        dest = folder / name
        tmp.replace(dest)
        if replace_parts:
            for old in replace_parts:
                old.unlink(missing_ok=True)
        return str(dest)

    def _record_file(self, path: str, payload: pl.DataFrame, ts_col: str, *, kind: str) -> None:
        min_ts = payload.select(pl.col(ts_col).min()).item()
        max_ts = payload.select(pl.col(ts_col).max()).item()
        size = Path(path).stat().st_size if Path(path).exists() else 0
        with self._duck_lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO files (path, bytes, row_count, min_ts, max_ts, kind)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [path, size, payload.height, min_ts, max_ts, kind],
            )
            self._conn.commit()

    def _delete_file_row(self, path: str) -> None:
        with self._duck_lock:
            self._conn.execute("DELETE FROM files WHERE path = ?", [path])
            self._conn.commit()

    def _bar_paths(
        self,
        instrument_id: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[str]:
        try:
            asset_class, venue, symbol = parse_instrument_id(instrument_id)
        except Exception:
            return []
        hive = symbol.replace("/", "-").replace(":", "-")
        base = (
            self.root
            / "bars"
            / f"asset_class={asset_class}"
            / f"venue={venue}"
            / f"symbol={hive}"
            / f"timeframe={timeframe}"
        )
        if not base.exists():
            return []
        paths: list[str] = []
        cursor = datetime(start.year, start.month, 1, tzinfo=UTC)
        last = datetime(end.year, end.month, 1, tzinfo=UTC)
        while cursor <= last:
            folder = base / f"year={cursor.year:04d}" / f"month={cursor.month:02d}"
            paths.extend(str(p) for p in folder.glob("part-*.parquet"))
            if cursor.month == 12:
                cursor = datetime(cursor.year + 1, 1, 1, tzinfo=UTC)
            else:
                cursor = datetime(cursor.year, cursor.month + 1, 1, tzinfo=UTC)
        return paths

    def _has_coarse(self, instrument_id: str, source: str) -> bool:
        return any(self.coverage(instrument_id, tf, source) for tf in ("1h", "1d"))

    def _upsert_snapshot_poll(self, chain: str, pair: str, source: str, polled: datetime) -> None:
        iid = f"DEX:{chain}:{pair}"
        with self._duck_lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO snapshot_polls (instrument_id, source, polled_at_utc)
                VALUES (?, ?, ?)
                """,
                [iid, source, polled],
            )
            self._conn.commit()


def _prepare_bars(bars: pl.DataFrame) -> pl.DataFrame:
    frame = bars
    missing = [c for c in BAR_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"write_batch missing columns: {missing}")
    frame = frame.with_columns(pl.col("ts_utc").dt.convert_time_zone("UTC"))
    frame = frame.with_columns(
        pl.col("ts_utc").dt.year().alias("year"),
        pl.col("ts_utc").dt.month().alias("month"),
    )
    return _dedup_bars(frame)


def _dedup_bars(frame: pl.DataFrame) -> pl.DataFrame:
    if "ts_utc" not in frame.columns:
        return frame
    if frame.schema["ts_utc"] != pl.Datetime("us", "UTC"):
        frame = frame.with_columns(pl.col("ts_utc").dt.convert_time_zone("UTC"))
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


def _partition_dirs(bars_root: Path) -> list[Path]:
    found: list[Path] = []
    for month_dir in bars_root.glob("asset_class=*/*/*/*/*/*"):
        if month_dir.is_dir() and month_dir.name.startswith("month="):
            found.append(month_dir)
    return found


def _year_month(folder: Path) -> tuple[int, int]:
    year = int(folder.parent.name.split("=")[1])
    month = int(folder.name.split("=")[1])
    return year, month


def _month_end_utc(year: int, month: int) -> datetime:
    if month == 12:
        return datetime(year + 1, 1, 1, tzinfo=UTC) - timedelta(microseconds=1)
    return datetime(year, month + 1, 1, tzinfo=UTC) - timedelta(microseconds=1)
