"""Load a long-format Panel from BarReader. Inner-join timestamps; no fetch."""

from __future__ import annotations

from datetime import UTC, date, datetime, time

import polars as pl

from backtester.exceptions import MissingCoverageError
from backtester.query.reader import BarReader
from backtester.store.interface import BarStore
from backtester.strategy.spec import Spec, UniverseRow
from backtester.timeframes import Timeframe

_BAR_COLS = ["ts_utc", "instrument_id", "open", "high", "low", "close", "volume"]
_NYSE_CALENDARS = frozenset({"nyse", "xnys", "us_equity"})
_ALWAYS_SESSION = frozenset({"native_24_7", "crypto", "24/7", "utc"})


def ingest_hint(instrument_id: str, timeframe: Timeframe, start: datetime, source: str) -> str:
    """Return the ingest command that would collect this series."""
    day = start.astimezone(UTC).date().isoformat()
    return f"backtester ingest {instrument_id} --tf {timeframe} --from {day} --source {source}"


def load_panel(spec: Spec, store: BarStore) -> pl.DataFrame:
    """Read Universe Bars, align on timestamp, attach Spec metadata.

    Coverage holes fail with an ingest command. The engine does not fetch.

    Args:
        spec: Shared Universe / window / source.
        store: BarStore behind BarReader.

    Returns:
        Long Panel: OHLCV, ``asset_class``, ``calendar``, ``fee_bps``, optional
        ``min_adv``, ``is_session``, ``is_decision``.

    Raises:
        MissingCoverageError: A name has no overlapping Coverage or zero Bars.
    """
    reader = BarReader(store)
    frames: list[pl.DataFrame] = []
    ts_frames: list[pl.DataFrame] = []
    for row in spec.universe:
        source = row.source or spec.source
        _require_coverage(store, row.id, spec.timeframe, source, spec.from_, spec.end)
        got = reader.read(
            row.id,
            spec.timeframe,
            spec.from_,
            spec.end,
            source=source,
        ).collect()
        if "instrument_id" not in got.columns:
            got = got.with_columns(pl.lit(row.id).alias("instrument_id"))
        keep = [c for c in _BAR_COLS if c in got.columns]
        frame = got.select(keep)
        if frame.height == 0:
            raise MissingCoverageError(
                f"missing Coverage for {row.id} {spec.timeframe} {source}; run:\n"
                f"{ingest_hint(row.id, spec.timeframe, spec.from_, source)}"
            )
        frames.append(frame)
        ts_frames.append(frame.select("ts_utc").unique())

    common = ts_frames[0]
    for extra in ts_frames[1:]:
        common = common.join(extra, on="ts_utc", how="inner")
    panel = pl.concat(frames, how="vertical").join(common, on="ts_utc", how="inner")
    if panel.height == 0:
        ids = ", ".join(r.id for r in spec.universe)
        raise MissingCoverageError(
            f"inner-join timestamps are empty for Universe [{ids}]; Coverage windows do not overlap"
        )

    meta = pl.DataFrame(
        {
            "instrument_id": [r.id for r in spec.universe],
            "asset_class": [r.class_ for r in spec.universe],
            "calendar": [r.calendar for r in spec.universe],
            "fee_bps": [float(r.fee_bps) for r in spec.universe],
            "min_adv": [r.min_adv for r in spec.universe],
        }
    )
    panel = panel.join(meta, on="instrument_id", how="left")
    panel = _attach_session_flags(panel, spec.universe)
    return panel.sort(["instrument_id", "ts_utc"])


def _require_coverage(
    store: BarStore,
    instrument_id: str,
    timeframe: Timeframe,
    source: str,
    start: datetime,
    end: datetime,
) -> None:
    ranges = store.coverage(instrument_id, timeframe, source)
    if not ranges:
        raise MissingCoverageError(
            f"missing Coverage for {instrument_id} {timeframe} {source}; run:\n"
            f"{ingest_hint(instrument_id, timeframe, start, source)}"
        )
    overlaps = any(not (item.end_utc < start or item.start_utc > end) for item in ranges)
    if not overlaps:
        raise MissingCoverageError(
            f"missing Coverage for {instrument_id} {timeframe} {source}; run:\n"
            f"{ingest_hint(instrument_id, timeframe, start, source)}"
        )


def _attach_session_flags(panel: pl.DataFrame, universe: list[UniverseRow]) -> pl.DataFrame:
    """Mark session/decision rows. Crypto is always in session; NYSE uses XNYS."""
    nyse_ids = {row.id for row in universe if row.calendar.lower() in _NYSE_CALENDARS}
    always_ids = {
        row.id for row in universe if row.calendar.lower() in _ALWAYS_SESSION or row.class_ in {"crypto", "dex"}
    }
    is_session = pl.lit(True)
    if nyse_ids:
        holidays = _nyse_holiday_dates(panel.filter(pl.col("instrument_id").is_in(list(nyse_ids))))
        nyse_session = (pl.col("ts_utc").dt.weekday() <= 5) & (~pl.col("ts_utc").dt.date().is_in(holidays))
        is_session = (
            pl.when(pl.col("instrument_id").is_in(list(always_ids)))
            .then(pl.lit(True))
            .when(pl.col("instrument_id").is_in(list(nyse_ids)))
            .then(nyse_session)
            .otherwise(pl.lit(True))
        )
    return panel.with_columns(
        is_session.alias("is_session"),
        is_session.alias("is_decision"),
    )


def _nyse_holiday_dates(frame: pl.DataFrame) -> list[date]:
    if frame.height == 0:
        return []
    from backtester.calendar.session import SessionCalendar

    cal = SessionCalendar()
    dates = frame.select(pl.col("ts_utc").dt.date().unique()).get_column("ts_utc").to_list()
    holidays: list[date] = []
    for day in dates:
        ts = datetime.combine(day, time.min, tzinfo=UTC)
        if cal.is_holiday(ts):
            holidays.append(day)
    return holidays
