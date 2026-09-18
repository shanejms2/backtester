"""VendorBar → Bar. One place for timestamps, OHLC invariants, is_partial."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import polars as pl

from backtester.calendar.display import ensure_utc
from backtester.domain import Instrument, PairSnapshot, Quality, VendorBar
from backtester.store.parquet_duckdb import empty_bars
from backtester.timeframes import TIMEFRAME_DELTA, Timeframe

NY_TZ = ZoneInfo("America/New_York")


def vendor_ts_to_utc(bar: VendorBar, instrument: Instrument) -> datetime:
    """Convert a vendor timestamp to timezone-aware UTC bar open.

    Args:
        bar: Vendor row. ``ts_kind`` selects the conversion.
        instrument: Supplies the Session zone for naive Eastern / session dates.

    Returns:
        Aware UTC datetime (bar open).
    """
    ts = bar.ts
    if bar.ts_kind == "utc":
        return ensure_utc(ts)
    session = ZoneInfo(instrument.session_tz)
    if bar.ts_kind == "session_date":
        # Date (or midnight) in the venue Session: equity daily → RTH open 09:30.
        local_date = ts.astimezone(session).date() if ts.tzinfo is not None else ts.date()
        if instrument.asset_class == "equity":
            local = datetime(local_date.year, local_date.month, local_date.day, 9, 30, tzinfo=session)
        else:
            local = datetime(local_date.year, local_date.month, local_date.day, 0, 0, tzinfo=session)
        return local.astimezone(UTC)
    # naive_session: Alpha Vantage Eastern clock strings.
    local = ts.replace(tzinfo=session) if ts.tzinfo is None else ts.astimezone(session)
    return local.astimezone(UTC)


def ohlc_valid(open_: float, high: float, low: float, close: float) -> bool:
    """Return True if OHLC invariants and finiteness hold."""
    values = (open_, high, low, close)
    if not all(math.isfinite(v) for v in values):
        return False
    return low <= min(open_, close) and high >= max(open_, close)


def is_partial_bar(ts_utc: datetime, timeframe: Timeframe, now: datetime) -> bool:
    """True when the interval that opened at ``ts_utc`` has not yet closed."""
    close_at = ts_utc + TIMEFRAME_DELTA[timeframe]
    return close_at > ensure_utc(now)


def normalize_vendor_bars(
    vendor_bars: Sequence[VendorBar],
    instrument: Instrument,
    timeframe: Timeframe,
    *,
    now: datetime | None = None,
) -> pl.DataFrame:
    """Normalize, validate, and drop invalid Bars.

    Rejects non-finite OHLC, failed invariants, and timestamps in the future
    (beyond ``now`` + one interval). Last bar of a live range is ``is_partial``
    until the interval closes. Dedup is last-write-wins on ``ts_utc``.

    Args:
        vendor_bars: Adapter page.
        instrument: Target series.
        timeframe: Canonical Timeframe.
        now: Clock for partial / future checks. Defaults to utcnow.

    Returns:
        Polars DataFrame in the Store schema (possibly empty).
    """
    clock = ensure_utc(now or datetime.now(UTC))
    future_limit = clock + TIMEFRAME_DELTA[timeframe]
    rows: list[dict[str, object]] = []
    seen: dict[datetime, int] = {}
    for bar in vendor_bars:
        if bar.timeframe != timeframe:
            continue
        ts_utc = vendor_ts_to_utc(bar, instrument)
        if ts_utc > future_limit:
            continue
        if not ohlc_valid(bar.open, bar.high, bar.low, bar.close):
            continue
        if not math.isfinite(bar.volume):
            continue
        partial = bar.is_partial if bar.is_partial is not None else is_partial_bar(ts_utc, timeframe, clock)
        quality: str = "ok"
        row = {
            "instrument_id": instrument.instrument_id,
            "source": bar.source,
            "timeframe": timeframe,
            "ts_utc": ts_utc,
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
            "volume": float(bar.volume),
            "is_partial": bool(partial),
            "quality": quality,
        }
        if ts_utc in seen:
            rows[seen[ts_utc]] = row
        else:
            seen[ts_utc] = len(rows)
            rows.append(row)
    if not rows:
        return empty_bars()
    frame = pl.DataFrame(rows)
    return frame.with_columns(pl.col("ts_utc").dt.convert_time_zone("UTC")).sort("ts_utc")


def snapshot_quality(price_usd: float | None, liquidity_usd: float | None) -> Quality:
    """Low-liquidity or missing USD price → ``suspect``."""
    if price_usd is None or not math.isfinite(price_usd):
        return "suspect"
    if liquidity_usd is None or liquidity_usd <= 0:
        return "suspect"
    return "ok"


def snapshots_to_frame(snapshots: Sequence[PairSnapshot]) -> pl.DataFrame:
    """PairSnapshots as a Polars frame. Never derived from priceChange windows."""
    if not snapshots:
        return pl.DataFrame()
    return pl.DataFrame([s.model_dump() for s in snapshots])
