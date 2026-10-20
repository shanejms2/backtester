"""Load a long-format Panel from BarReader. Inner-join timestamps; no fetch."""

from __future__ import annotations

from datetime import UTC, date, datetime, time

import polars as pl

from backtester.calendar.session import IST, NY_TZ, SessionCalendar
from backtester.exceptions import MissingCoverageError
from backtester.query.reader import BarReader
from backtester.store.interface import BarStore
from backtester.strategy.spec import Spec, UniverseRow
from backtester.timeframes import Timeframe

_BAR_COLS = ["ts_utc", "instrument_id", "open", "high", "low", "close", "volume", "adj_close"]
_NYSE_CALENDARS = frozenset({"nyse", "xnys", "us_equity"})
_NSE_CALENDARS = frozenset({"nse", "xnse", "india"})
_ALWAYS_SESSION = frozenset({"native_24_7", "crypto", "24/7", "utc"})
_CRYPTO_CLASSES = frozenset({"crypto", "dex"})
_IRX_PERCENT = 100.0
_IRX_DAYS = 365.0


def ingest_hint(instrument_id: str, timeframe: Timeframe, start: datetime, source: str) -> str:
    """Return the ingest command that would collect this series."""
    day = start.astimezone(UTC).date().isoformat()
    return f"backtester ingest {instrument_id} --tf {timeframe} --from {day} --source {source}"


def load_panel(spec: Spec, store: BarStore) -> pl.DataFrame:
    """Read Universe Bars, align on timestamp, attach Spec metadata.

    Coverage holes fail with an ingest command. The engine does not fetch.

    ``clock: nyse_1600`` samples each name onto NYSE session days (16:00 ET
    close, next-session open via the following row). ``clock: native`` inner-
    joins native ``ts_utc``. ``cash_rate_id`` is a yield overlay, not a name.

    Args:
        spec: Shared Universe / window / source.
        store: BarStore behind BarReader.

    Returns:
        Long Panel: OHLCV, ``adj_close``, ``asset_class``, ``calendar``,
        ``fee_bps``, optional ``min_adv`` / ``role``, ``is_session``,
        ``is_decision``, ``daily_tbill``.

    Raises:
        MissingCoverageError: A name has no overlapping Coverage or zero Bars.
    """
    reader = BarReader(store)
    frames: list[pl.DataFrame] = []
    for row in spec.universe:
        source = row.source or spec.source
        tf = _row_timeframe(row, spec)
        _require_coverage(store, row.id, tf, source, spec.from_, spec.end)
        got = reader.read(
            row.id,
            tf,
            spec.from_,
            spec.end,
            source=source,
        ).collect()
        if "instrument_id" not in got.columns:
            got = got.with_columns(pl.lit(row.id).alias("instrument_id"))
        got = _with_adj_close(got)
        keep = [c for c in _BAR_COLS if c in got.columns]
        frame = got.select(keep)
        if frame.height == 0:
            raise MissingCoverageError(
                f"missing Coverage for {row.id} {tf} {source}; run:\n{ingest_hint(row.id, tf, spec.from_, source)}"
            )
        frames.append(frame)

    if spec.clock == "nyse_1600":
        panel = _align_session(frames, spec, _decision_grid(spec.from_, spec.end))
    elif spec.clock == "xnse":
        panel = _align_session(frames, spec, _decision_grid_xnse(spec.from_, spec.end))
    else:
        panel = _align_native(frames)
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
            "role": [r.role for r in spec.universe],
            "currency": [r.currency.upper() for r in spec.universe],
        }
    )
    panel = panel.join(meta, on="instrument_id", how="left")
    panel = _convert_to_usd(panel, spec, store)
    panel = _attach_session_flags(panel, spec)
    panel = _attach_cash_rate(panel, spec, store)
    if spec.total_return:
        panel = _scale_ohlc_to_adj(panel)
    return panel.sort(["instrument_id", "ts_utc"])


def _row_timeframe(row: UniverseRow, spec: Spec) -> Timeframe:
    if spec.clock != "nyse_1600":
        return spec.timeframe
    if row.class_ in _CRYPTO_CLASSES:
        return "1h"
    return "1d"


def _with_adj_close(frame: pl.DataFrame) -> pl.DataFrame:
    if "adj_close" not in frame.columns:
        return frame.with_columns(pl.col("close").alias("adj_close"))
    return frame.with_columns(pl.col("adj_close").fill_null(pl.col("close")))


def _align_native(frames: list[pl.DataFrame]) -> pl.DataFrame:
    ts_frames = [frame.select("ts_utc").unique() for frame in frames]
    common = ts_frames[0]
    for extra in ts_frames[1:]:
        common = common.join(extra, on="ts_utc", how="inner")
    return pl.concat(frames, how="vertical").join(common, on="ts_utc", how="inner")


def _align_session(frames: list[pl.DataFrame], spec: Spec, grid: pl.DataFrame) -> pl.DataFrame:
    """Sample each name onto ``grid``. A closed name keeps a null-OHLC row."""
    if grid.height == 0:
        return frames[0].clear()
    sampled = [
        (
            _sample_crypto_1h(frame, row.id, grid)
            if row.class_ in _CRYPTO_CLASSES
            else _sample_equity_1d(frame, row.id, grid, _row_session_tz(row))
        )
        for frame, row in zip(frames, spec.universe, strict=True)
    ]
    panel = pl.concat(sampled, how="diagonal_relaxed")
    alive = panel.group_by("ts_utc").agg(pl.col("close").count().alias("n_close"))
    keep = alive.filter(pl.col("n_close") > 0).select("ts_utc")
    return panel.join(keep, on="ts_utc", how="inner")


def _decision_grid(start: datetime, end: datetime) -> pl.DataFrame:
    dates = SessionCalendar().session_dates(start, end)
    if not dates:
        return pl.DataFrame(
            schema={
                "session_date": pl.Date,
                "ts_utc": pl.Datetime("us", "UTC"),
                "rth_open_utc": pl.Datetime("us", "UTC"),
            }
        )
    rows = [
        {
            "session_date": day,
            "ts_utc": datetime(day.year, day.month, day.day, 16, 0, tzinfo=NY_TZ).astimezone(UTC),
            "rth_open_utc": datetime(day.year, day.month, day.day, 9, 30, tzinfo=NY_TZ).astimezone(UTC),
        }
        for day in dates
    ]
    return pl.DataFrame(rows).with_columns(
        pl.col("ts_utc").dt.convert_time_zone("UTC"),
        pl.col("rth_open_utc").dt.convert_time_zone("UTC"),
    )


def _decision_grid_xnse(start: datetime, end: datetime) -> pl.DataFrame:
    """NSE decision rows stamped 15:30 IST, with the 09:15 IST open on the row."""
    dates = SessionCalendar().xnse_session_dates(start, end)
    if not dates:
        return pl.DataFrame(
            schema={
                "session_date": pl.Date,
                "ts_utc": pl.Datetime("us", "UTC"),
                "rth_open_utc": pl.Datetime("us", "UTC"),
            }
        )
    rows = [
        {
            "session_date": day,
            "ts_utc": datetime(day.year, day.month, day.day, 15, 30, tzinfo=IST).astimezone(UTC),
            "rth_open_utc": datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST).astimezone(UTC),
        }
        for day in dates
    ]
    return pl.DataFrame(rows).with_columns(
        pl.col("ts_utc").dt.convert_time_zone("UTC"),
        pl.col("rth_open_utc").dt.convert_time_zone("UTC"),
    )


def _scale_ohlc_to_adj(panel: pl.DataFrame) -> pl.DataFrame:
    """Put OHLC on the adjusted-close basis so splits and dividends stay in P&L."""
    if "adj_close" not in panel.columns or "close" not in panel.columns:
        return panel
    factor = (
        pl.when(pl.col("close").is_not_null() & (pl.col("close").abs() > 1e-12) & pl.col("adj_close").is_not_null())
        .then(pl.col("adj_close") / pl.col("close"))
        .otherwise(1.0)
    )
    cols = [c for c in ("open", "high", "low", "close") if c in panel.columns]
    return panel.with_columns([(pl.col(c) * factor).alias(c) for c in cols])


def _row_session_tz(row: UniverseRow) -> str:
    if row.calendar.lower() in _NSE_CALENDARS:
        return "Asia/Kolkata"
    if row.class_ in _CRYPTO_CLASSES:
        return "UTC"
    return "America/New_York"


def _sample_equity_1d(
    frame: pl.DataFrame,
    instrument_id: str,
    grid: pl.DataFrame,
    session_tz: str,
) -> pl.DataFrame:
    native = (
        _with_adj_close(frame)
        .rename({"ts_utc": "native_ts"})
        .with_columns(
            pl.col("native_ts").dt.convert_time_zone(session_tz).dt.date().alias("session_date"),
            pl.lit(instrument_id).alias("instrument_id"),
        )
    )
    joined = grid.join(native, on="session_date", how="left")
    return joined.select(
        "ts_utc",
        pl.lit(instrument_id).alias("instrument_id"),
        "open",
        "high",
        "low",
        "close",
        "volume",
        "adj_close",
    )


def _sample_crypto_1h(frame: pl.DataFrame, instrument_id: str, grid: pl.DataFrame) -> pl.DataFrame:
    native = (
        _with_adj_close(frame)
        .rename({"ts_utc": "native_ts"})
        .with_columns(
            pl.col("native_ts").dt.convert_time_zone("America/New_York").dt.date().alias("session_date"),
            (pl.col("native_ts") + pl.duration(hours=1)).alias("bar_close_ts"),
            pl.lit(instrument_id).alias("instrument_id"),
        )
    )
    sess = (
        native.join(grid, on="session_date", how="inner")
        .filter(
            (pl.col("native_ts") + pl.duration(hours=1) > pl.col("rth_open_utc"))
            & (pl.col("bar_close_ts") <= pl.col("ts_utc"))
        )
        .sort(["session_date", "native_ts"])
    )
    agg = sess.group_by("session_date").agg(
        pl.col("open").first(),
        pl.col("high").max(),
        pl.col("low").min(),
        pl.col("close").last(),
        pl.col("volume").sum(),
        pl.col("adj_close").last(),
        pl.col("ts_utc").first(),
    )
    out = grid.select("session_date", "ts_utc").join(agg.drop("ts_utc"), on="session_date", how="left")
    return out.select(
        "ts_utc",
        pl.lit(instrument_id).alias("instrument_id"),
        "open",
        "high",
        "low",
        "close",
        "volume",
        "adj_close",
    )


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


def _attach_session_flags(panel: pl.DataFrame, spec: Spec) -> pl.DataFrame:
    """Mark session/decision rows. Clock grids are already decision bars."""
    if spec.clock in {"nyse_1600", "xnse"}:
        return panel.with_columns(
            pl.lit(True).alias("is_session"),
            pl.lit(True).alias("is_decision"),
        )
    universe = spec.universe
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


def _convert_to_usd(panel: pl.DataFrame, spec: Spec, store: BarStore) -> pl.DataFrame:
    """Convert non-USD OHLC / adj_close into USD using Spec ``fx`` quotes.

    Yahoo ``INR=X`` is rupees per USD, so USD = local / fx_close. FX is not a
    Universe name and is never clustered.
    """
    needed = sorted({row.currency.upper() for row in spec.universe if row.currency.upper() != "USD"})
    if not needed:
        return panel
    work = panel
    price_cols = [c for c in ("open", "high", "low", "close", "adj_close") if c in work.columns]
    for ccy in needed:
        fx_id = spec.fx.get(ccy)
        if not fx_id:
            raise MissingCoverageError(f"Spec.fx missing a USD quote for currency {ccy}")
        source = spec.source
        tf: Timeframe = "1d"
        _require_coverage(store, fx_id, tf, source, spec.from_, spec.end)
        got = BarReader(store).read(fx_id, tf, spec.from_, spec.end, source=source).collect()
        if got.height == 0:
            raise MissingCoverageError(
                f"missing Coverage for {fx_id} {tf} {source}; run:\n{ingest_hint(fx_id, tf, spec.from_, source)}"
            )
        fx = (
            got.select(pl.col("ts_utc").alias("fx_ts"), pl.col("close").alias("fx_close"))
            .sort("fx_ts")
            .with_columns(pl.col("fx_ts").dt.convert_time_zone("UTC"))
        )
        stamps = work.select("ts_utc").unique().sort("ts_utc")
        joined = stamps.join_asof(fx, left_on="ts_utc", right_on="fx_ts", strategy="backward")
        joined = joined.select("ts_utc", "fx_close")
        work = work.join(joined, on="ts_utc", how="left")
        fx_ok = pl.col("fx_close").is_not_null() & (pl.col("fx_close").abs() > 0)
        is_ccy = pl.col("currency") == ccy
        for col in price_cols:
            work = work.with_columns(
                pl.when(is_ccy & fx_ok).then(pl.col(col) / pl.col("fx_close")).otherwise(pl.col(col)).alias(col)
            )
        work = work.drop("fx_close")
    return work


def _attach_cash_rate(panel: pl.DataFrame, spec: Spec, store: BarStore) -> pl.DataFrame:
    """Broadcast daily T-bill return. Yahoo IRX close is an annualized percent.

    ``daily_tbill = close / 100 / 365``. Missing ``cash_rate_id`` yields 0.
    The rate series is not a Universe name and is never clustered.
    """
    if not spec.cash_rate_id:
        return panel.with_columns(pl.lit(0.0).alias("daily_tbill"))
    source = spec.source
    tf: Timeframe = "1d"
    _require_coverage(store, spec.cash_rate_id, tf, source, spec.from_, spec.end)
    got = BarReader(store).read(spec.cash_rate_id, tf, spec.from_, spec.end, source=source).collect()
    if got.height == 0:
        raise MissingCoverageError(
            f"missing Coverage for {spec.cash_rate_id} {tf} {source}; run:\n"
            f"{ingest_hint(spec.cash_rate_id, tf, spec.from_, source)}"
        )
    rates = (
        got.select(
            pl.col("ts_utc").alias("rate_ts"),
            (pl.col("close") / _IRX_PERCENT / _IRX_DAYS).alias("daily_tbill"),
        )
        .sort("rate_ts")
        .with_columns(pl.col("rate_ts").dt.convert_time_zone("UTC"))
    )
    stamps = panel.select("ts_utc").unique().sort("ts_utc")
    joined = stamps.join_asof(rates, left_on="ts_utc", right_on="rate_ts", strategy="backward")
    joined = joined.select("ts_utc", pl.col("daily_tbill").fill_null(0.0))
    return panel.join(joined, on="ts_utc", how="left").with_columns(pl.col("daily_tbill").fill_null(0.0))


def _nyse_holiday_dates(frame: pl.DataFrame) -> list[date]:
    if frame.height == 0:
        return []
    cal = SessionCalendar()
    dates = frame.select(pl.col("ts_utc").dt.date().unique()).get_column("ts_utc").to_list()
    holidays: list[date] = []
    for day in dates:
        ts = datetime.combine(day, time.min, tzinfo=UTC)
        if cal.is_holiday(ts):
            holidays.append(day)
    return holidays
