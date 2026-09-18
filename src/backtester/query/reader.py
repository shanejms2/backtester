"""BarReader: tz projection and optional resample. The query seam for later backtests."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

import polars as pl

from backtester.calendar.display import parse_display_tz, require_zone, to_utc
from backtester.store.interface import BarStore
from backtester.timeframes import TIMEFRAME_POLARS, Timeframe, parse_timeframe


class BarReader:
    """Read stored Bars as a Polars LazyFrame.

    Always includes ``ts_utc``. When ``tz`` is set, adds zone-aware ``ts_local``
    and ``utc_offset`` minutes (DST visible). Optional ``resample`` buckets in
    the display zone and is a derived series — it never rewrites stored ``1d``.
    """

    def __init__(self, store: BarStore) -> None:
        """Attach to a Store.

        Args:
            store: BarStore implementation (Parquet+DuckDB in v1).
        """
        self._store = store

    def read(
        self,
        instrument_id: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        *,
        tz: str | None = None,
        resample: str | None = None,
        columns: Sequence[str] | None = None,
        source: str | None = None,
    ) -> pl.LazyFrame:
        """Scan Bars, optionally project display TZ and resample.

        Args:
            instrument_id: Stable Catalog id.
            timeframe: Native stored Timeframe to scan.
            start: Range start. Naive values are interpreted in ``tz`` (or UTC).
            end: Range end (inclusive of last bar open).
            tz: IANA display zone. ``None`` keeps UTC-only (no ``ts_local``).
            resample: Canonical Timeframe to bucket into (``1h``, ``1d``, …).
            columns: Extra columns to keep besides ``ts_utc`` (and display cols).
            source: Optional vendor filter.

        Returns:
            LazyFrame. Collect at the call site.

        Raises:
            InvalidTimezoneError: Abbreviation, offset, or unknown IANA name.
        """
        zone = parse_display_tz(tz)
        start_utc, end_utc = _range_to_utc(start, end, zone)
        wanted = None if columns is None else list(columns)
        lf = self._store.scan(
            instrument_id,
            timeframe,
            start_utc,
            end_utc,
            columns=wanted,
            source=source,
        )
        if zone is not None:
            lf = _attach_local(lf, zone)
        if resample:
            target = parse_timeframe(resample)
            lf = _resample(lf, target, zone)
        return lf


def _range_to_utc(
    start: datetime,
    end: datetime,
    zone: ZoneInfo | None,
) -> tuple[datetime, datetime]:
    interpret = zone or ZoneInfo("UTC")
    return to_utc(start, interpret), to_utc(end, interpret)


def _attach_local(lf: pl.LazyFrame, zone: ZoneInfo) -> pl.LazyFrame:
    name = str(zone)
    return lf.with_columns(
        pl.col("ts_utc").dt.convert_time_zone(name).alias("ts_local"),
    ).with_columns(
        (
            (
                pl.col("ts_local").dt.base_utc_offset().dt.total_minutes()
                + pl.col("ts_local").dt.dst_offset().dt.total_minutes()
            ).cast(pl.Int32)
        ).alias("utc_offset"),
    )


def _resample(lf: pl.LazyFrame, target: Timeframe, zone: ZoneInfo | None) -> pl.LazyFrame:
    every = TIMEFRAME_POLARS[target]
    ts_col = "ts_local" if zone is not None else "ts_utc"
    grouped = (
        lf.sort(ts_col)
        .group_by_dynamic(ts_col, every=every, closed="left", label="left")
        .agg(
            pl.col("open").first(),
            pl.col("high").max(),
            pl.col("low").min(),
            pl.col("close").last(),
            pl.col("volume").sum(),
            pl.col("instrument_id").last(),
            pl.col("source").last(),
            pl.col("is_partial").last(),
            pl.col("quality").last(),
            pl.col("ts_utc").first().alias("ts_utc"),
        )
        .with_columns(pl.lit(target).alias("timeframe"))
    )
    if zone is not None and ts_col == "ts_local":
        grouped = grouped.with_columns(
            (
                (
                    pl.col("ts_local").dt.base_utc_offset().dt.total_minutes()
                    + pl.col("ts_local").dt.dst_offset().dt.total_minutes()
                ).cast(pl.Int32)
            ).alias("utc_offset"),
        )
    return grouped


# Touch require_zone so CLI can share the same fail-fast path.
__all__ = ["BarReader", "require_zone"]
