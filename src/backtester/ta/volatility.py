"""Volatility Indicators: ATR, Bollinger, Keltner, Donchian, rolling stdev, ewm_std."""

from __future__ import annotations

import polars as pl

from backtester.ta._core import (
    ema_expr,
    eval_exprs,
    eval_series,
    require_columns,
    require_length,
    rma_expr,
    rolling_max,
    rolling_mean,
    rolling_min,
    rolling_std,
    safe_div,
    true_range,
)


def atr(panel: pl.DataFrame, length: int = 14) -> pl.Series:
    """Wilder Average True Range. First ``length - 1`` bars are null.

    Args:
        panel: Long-format Panel with OHLC.
        length: Wilder period.

    Returns:
        Series named ``atr``.
    """
    require_columns(panel, "high", "low", "close")
    require_length(length)
    return eval_series(panel, rma_expr(true_range(), length), "atr")


def bbands(
    panel: pl.DataFrame,
    length: int = 20,
    n_std: float = 2.0,
    col: str = "close",
    ddof: int = 0,
) -> pl.DataFrame:
    """Bollinger Bands: mid (SMA), upper, lower, %B, and bandwidth.

    Standard deviation uses ``ddof=0`` (population) unless overridden.

    Args:
        panel: Long-format Panel.
        length: SMA / stdev window.
        n_std: Band width in standard deviations.
        col: Source column.
        ddof: Delta degrees of freedom for stdev.

    Returns:
        Columns ``mid``, ``upper``, ``lower``, ``percent_b``, ``width``.
    """
    require_columns(panel, col)
    require_length(length)
    mid = rolling_mean(pl.col(col), length)
    sd = rolling_std(pl.col(col), length, ddof=ddof)
    upper = mid + n_std * sd
    lower = mid - n_std * sd
    return eval_exprs(
        panel,
        {
            "mid": mid,
            "upper": upper,
            "lower": lower,
            "percent_b": safe_div(pl.col(col) - lower, upper - lower),
            "width": safe_div(upper - lower, mid),
        },
    )


def keltner(
    panel: pl.DataFrame,
    length: int = 20,
    multiplier: float = 2.0,
    atr_length: int | None = None,
    col: str = "close",
) -> pl.DataFrame:
    """Keltner Channels: EMA mid ± ``multiplier * ATR``.

    Args:
        panel: Long-format Panel with OHLC.
        length: EMA period for the midline.
        multiplier: Band width in ATRs.
        atr_length: ATR period; defaults to ``length``.
        col: Source column for the EMA.

    Returns:
        Columns ``mid``, ``upper``, ``lower``.
    """
    require_columns(panel, col, "high", "low", "close")
    require_length(length)
    n_atr = length if atr_length is None else atr_length
    require_length(n_atr, name="atr_length")
    if multiplier <= 0:
        raise ValueError(f"multiplier must be > 0, got {multiplier}")
    mid = ema_expr(pl.col(col), length)
    band = multiplier * rma_expr(true_range(), n_atr)
    return eval_exprs(panel, {"mid": mid, "upper": mid + band, "lower": mid - band})


def donchian(panel: pl.DataFrame, length: int = 20) -> pl.DataFrame:
    """Donchian channel of highs and lows, current bar included.

    Args:
        panel: Long-format Panel with high/low.
        length: Window in bars.

    Returns:
        Columns ``mid``, ``upper``, ``lower``.
    """
    require_columns(panel, "high", "low")
    require_length(length)
    upper = rolling_max(pl.col("high"), length)
    lower = rolling_min(pl.col("low"), length)
    return eval_exprs(panel, {"upper": upper, "lower": lower, "mid": (upper + lower) / 2.0})


def stdev(panel: pl.DataFrame, length: int = 20, col: str = "close", ddof: int = 0) -> pl.Series:
    """Rolling standard deviation. First ``length - 1`` bars are null.

    Args:
        panel: Long-format Panel.
        length: Window in bars.
        col: Source column.
        ddof: Delta degrees of freedom (0 = population).

    Returns:
        Series named ``stdev``.
    """
    require_columns(panel, col)
    require_length(length)
    return eval_series(panel, rolling_std(pl.col(col), length, ddof=ddof), "stdev")


def ewm_std(panel: pl.DataFrame, half_life: float, col: str = "close") -> pl.Series:
    """Causal exponential-weighted standard deviation per Instrument.

    ``half_life`` is in bars: an observation's relative weight halves after
    that many periods. Includes the current bar. The first bar is null
    (undefined std of a single point).

    Args:
        panel: Long-format Panel.
        half_life: EWM half-life in bars. Must be ``> 0``.
        col: Source column.

    Returns:
        Series named ``ewm_std``.
    """
    require_columns(panel, col)
    if half_life <= 0:
        raise ValueError(f"half_life must be > 0, got {half_life}")
    expr = (
        pl.col(col).ewm_std(half_life=half_life, adjust=False, min_samples=2, ignore_nulls=True).over("instrument_id")
    )
    return eval_series(panel, expr, "ewm_std")
