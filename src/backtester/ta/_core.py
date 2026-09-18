"""Shared Panel helpers for causal, per-Instrument Indicators."""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import polars as pl

GROUP = "instrument_id"
TS = "ts_utc"
_ROW = "_bt_ta_row"


def require_columns(panel: pl.DataFrame, *columns: str) -> None:
    """Raise if the Panel is missing identity, time, or requested columns.

    Args:
        panel: Long-format Bars (`instrument_id`, `ts_utc`, OHLCV).
        *columns: Extra columns required by the Indicator.

    Raises:
        ValueError: A required column is absent.
    """
    missing = [c for c in (GROUP, TS, *columns) if c not in panel.columns]
    if missing:
        raise ValueError(f"panel is missing required columns: {missing}")


def require_length(length: int, *, name: str = "length", minimum: int = 1) -> None:
    """Raise if a window length is not a positive integer.

    Args:
        length: Requested window.
        name: Parameter name in the error.
        minimum: Inclusive lower bound.

    Raises:
        ValueError: ``length`` is below ``minimum``.
    """
    if length < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {length}")


def group_idx() -> pl.Expr:
    """0-based row number within each ``instrument_id`` group."""
    return pl.int_range(0, pl.len()).over(GROUP)


def over(expr: pl.Expr) -> pl.Expr:
    """Apply ``expr`` independently per Instrument."""
    return expr.over(GROUP)


def shift(expr: pl.Expr, n: int = 1) -> pl.Expr:
    """Shift ``expr`` by ``n`` bars within each Instrument."""
    return expr.shift(n).over(GROUP)


def typical_price() -> pl.Expr:
    """(high + low + close) / 3."""
    return (pl.col("high") + pl.col("low") + pl.col("close")) / 3.0


def true_range() -> pl.Expr:
    """True range; the first bar of each Instrument is high − low."""
    prev_close = shift(pl.col("close"))
    high_low = pl.col("high") - pl.col("low")
    high_close = (pl.col("high") - prev_close).abs()
    low_close = (pl.col("low") - prev_close).abs()
    return pl.when(prev_close.is_null()).then(high_low).otherwise(pl.max_horizontal(high_low, high_close, low_close))


def ewm_seeded(expr: pl.Expr, length: int, *, alpha: float) -> pl.Expr:
    """Causal EWM with an SMA seed; leading bars are null.

    The first non-null output is the SMA of the first ``length`` non-null
    values of ``expr`` in the Instrument. After that the recurrence is
    ``y_t = alpha * x_t + (1 - alpha) * y_{t-1}`` (TA-Lib / Wilder seed).

    Args:
        expr: Values to smooth (already Instrument-grouped where needed).
        length: Seed window and the span implied by ``alpha``.
        alpha: Recurrence weight on the new observation.
    """
    sma = expr.rolling_mean(window_size=length, min_samples=length).over(GROUP)
    prev_sma = shift(sma)
    first_valid = sma.is_not_null() & prev_sma.is_null()
    seen = first_valid.cast(pl.Int8).fill_null(0).cum_sum().over(GROUP) > 0
    prepared = pl.when(first_valid).then(sma).when(seen).then(expr).otherwise(None)
    return prepared.ewm_mean(alpha=alpha, adjust=False, min_samples=1, ignore_nulls=True).over(GROUP)


def ema_expr(expr: pl.Expr, length: int) -> pl.Expr:
    """EMA with SMA seed; ``alpha = 2 / (length + 1)``."""
    return ewm_seeded(expr, length, alpha=2.0 / (length + 1))


def rma_expr(expr: pl.Expr, length: int) -> pl.Expr:
    """Wilder moving average with SMA seed; ``alpha = 1 / length``."""
    return ewm_seeded(expr, length, alpha=1.0 / length)


def rolling_mean(expr: pl.Expr, length: int) -> pl.Expr:
    """Causal rolling mean; first ``length - 1`` bars are null."""
    return expr.rolling_mean(window_size=length, min_samples=length).over(GROUP)


def rolling_sum(expr: pl.Expr, length: int) -> pl.Expr:
    """Causal rolling sum; first ``length - 1`` bars are null."""
    return expr.rolling_sum(window_size=length, min_samples=length).over(GROUP)


def rolling_std(expr: pl.Expr, length: int, *, ddof: int) -> pl.Expr:
    """Causal rolling standard deviation."""
    return expr.rolling_std(window_size=length, min_samples=length, ddof=ddof).over(GROUP)


def rolling_min(expr: pl.Expr, length: int) -> pl.Expr:
    """Causal rolling min."""
    return expr.rolling_min(window_size=length, min_samples=length).over(GROUP)


def rolling_max(expr: pl.Expr, length: int) -> pl.Expr:
    """Causal rolling max."""
    return expr.rolling_max(window_size=length, min_samples=length).over(GROUP)


def safe_div(num: pl.Expr, den: pl.Expr) -> pl.Expr:
    """Divide, returning null when the denominator is 0."""
    return pl.when(den == 0).then(None).otherwise(num / den)


def with_sorted_index(panel: pl.DataFrame) -> pl.DataFrame:
    """Attach a stable row id and sort by Instrument then time."""
    return panel.with_row_index(_ROW).sort([GROUP, TS, _ROW])


def eval_exprs(panel: pl.DataFrame, exprs: dict[str, pl.Expr]) -> pl.DataFrame:
    """Evaluate expressions on a time-sorted Panel; restore original row order.

    Args:
        panel: Long-format Bars.
        exprs: Output column name → expression.

    Returns:
        DataFrame with only ``exprs`` keys, aligned to ``panel`` rows.
    """
    numbered = with_sorted_index(panel)
    out = numbered.with_columns([expr.alias(name) for name, expr in exprs.items()])
    return out.sort(_ROW).select(list(exprs))


def eval_series(panel: pl.DataFrame, expr: pl.Expr, name: str) -> pl.Series:
    """Evaluate one expression and return it as a Series aligned to ``panel``."""
    return eval_exprs(panel, {name: expr})[name]


def session_day_expr(panel: pl.DataFrame) -> pl.Expr:
    """Calendar day used to reset session VWAP.

    Prefers ``session_date``, then a date-typed (or date-like string)
    ``calendar`` column, then an IANA zone in ``calendar`` applied to
    ``ts_utc``. Otherwise the UTC date of ``ts_utc``.
    """
    if "session_date" in panel.columns:
        return _to_date(pl.col("session_date"), panel.schema["session_date"])
    if "calendar" in panel.columns:
        dtype = panel.schema["calendar"]
        if dtype == pl.Date:
            return pl.col("calendar")
        if isinstance(dtype, pl.Datetime) or dtype == pl.Datetime:
            return pl.col("calendar").dt.date()
        if dtype in (pl.String, pl.Utf8, pl.Categorical):
            values = panel.get_column("calendar").drop_nulls().unique().to_list()
            if values and all(_looks_like_date(v) for v in values[:8]):
                return pl.col("calendar").str.to_date(strict=False)
            zones = [str(v) for v in values if _is_iana_zone(str(v))]
            if zones:
                expr: pl.Expr = _utc_date()
                for zone in zones:
                    expr = (
                        pl.when(pl.col("calendar") == zone)
                        .then(pl.col(TS).dt.convert_time_zone(zone).dt.date())
                        .otherwise(expr)
                    )
                return expr
    return _utc_date()


def _utc_date() -> pl.Expr:
    return pl.col(TS).dt.convert_time_zone("UTC").dt.date()


def _to_date(expr: pl.Expr, dtype: pl.DataType) -> pl.Expr:
    if dtype == pl.Date:
        return expr
    if isinstance(dtype, pl.Datetime) or dtype == pl.Datetime:
        return expr.dt.date()
    if dtype in (pl.String, pl.Utf8):
        return expr.str.to_date(strict=False)
    return expr.cast(pl.Date)


def _looks_like_date(value: object) -> bool:
    text = str(value)
    return len(text) >= 10 and text[4] == "-" and text[7] == "-"


def _is_iana_zone(name: str) -> bool:
    if name.upper() == "UTC":
        return True
    if "/" not in name:
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return False
    return True
