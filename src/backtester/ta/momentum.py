"""Momentum Indicators: RSI, Stochastic, Stochastic RSI, CCI, Williams %R, ROC, Momentum."""

from __future__ import annotations

import polars as pl

from backtester.ta._core import (
    GROUP,
    eval_exprs,
    eval_series,
    require_columns,
    require_length,
    rma_expr,
    rolling_max,
    rolling_mean,
    rolling_min,
    safe_div,
    shift,
    typical_price,
)


def rsi(panel: pl.DataFrame, length: int = 14, col: str = "close") -> pl.Series:
    """Wilder RSI. First ``length`` bars are null (needs ``length`` changes).

    Average gain/loss uses Wilder smoothing with an SMA seed.

    Args:
        panel: Long-format Panel.
        length: Lookback in bars.
        col: Source column.

    Returns:
        Series named ``rsi``, aligned to ``panel`` rows.
    """
    require_columns(panel, col)
    require_length(length)
    return eval_series(panel, _rsi_expr(pl.col(col), length), "rsi")


def stoch(panel: pl.DataFrame, k: int = 14, d: int = 3, smooth: int = 1) -> pl.DataFrame:
    """Stochastic oscillator.

    Raw %K uses the last ``k`` highs/lows. ``smooth > 1`` slows %K with an SMA.
    %D is an SMA of %K.

    Args:
        panel: Long-format Panel with OHLC.
        k: Fast %K lookback.
        d: %D smoothing length.
        smooth: Slowing SMA on %K (1 = fast stochastic).

    Returns:
        Columns ``k``, ``d``.
    """
    require_columns(panel, "high", "low", "close")
    require_length(k, name="k")
    require_length(d, name="d")
    require_length(smooth, name="smooth")
    lowest = rolling_min(pl.col("low"), k)
    highest = rolling_max(pl.col("high"), k)
    raw = 100.0 * safe_div(pl.col("close") - lowest, highest - lowest)
    k_s = rolling_mean(raw, smooth) if smooth > 1 else raw
    return eval_exprs(panel, {"k": k_s, "d": rolling_mean(k_s, d)})


def stoch_rsi(
    panel: pl.DataFrame,
    length: int = 14,
    k: int = 14,
    d: int = 3,
    col: str = "close",
) -> pl.DataFrame:
    """Stochastic RSI: stochastic of Wilder RSI, scaled 0–100.

    Args:
        panel: Long-format Panel.
        length: RSI period.
        k: Stochastic lookback on RSI.
        d: %D SMA length.
        col: Source column for RSI.

    Returns:
        Columns ``k``, ``d``.
    """
    require_columns(panel, col)
    require_length(length)
    require_length(k, name="k")
    require_length(d, name="d")
    r = _rsi_expr(pl.col(col), length)
    lowest = rolling_min(r, k)
    highest = rolling_max(r, k)
    raw = 100.0 * safe_div(r - lowest, highest - lowest)
    return eval_exprs(panel, {"k": raw, "d": rolling_mean(raw, d)})


def cci(panel: pl.DataFrame, length: int = 20) -> pl.Series:
    """Commodity Channel Index of typical price.

    ``CCI = (TP − SMA(TP)) / (0.015 * MAD)`` where MAD is the mean absolute
    deviation from that window's SMA.

    Args:
        panel: Long-format Panel with OHLC.
        length: Window in bars.

    Returns:
        Series named ``cci``.
    """
    require_columns(panel, "high", "low", "close")
    require_length(length)
    tp = typical_price()
    sma = rolling_mean(tp, length)
    devs = [(shift(tp, k) - sma).abs() for k in range(length)]
    mad = pl.sum_horizontal(*devs, ignore_nulls=False) / length
    return eval_series(panel, safe_div(tp - sma, 0.015 * mad), "cci")


def willr(panel: pl.DataFrame, length: int = 14) -> pl.Series:
    """Williams %R. Range is typically −100 to 0.

    Args:
        panel: Long-format Panel with OHLC.
        length: Lookback in bars.

    Returns:
        Series named ``willr``.
    """
    require_columns(panel, "high", "low", "close")
    require_length(length)
    highest = rolling_max(pl.col("high"), length)
    lowest = rolling_min(pl.col("low"), length)
    expr = -100.0 * safe_div(highest - pl.col("close"), highest - lowest)
    return eval_series(panel, expr, "willr")


def roc(panel: pl.DataFrame, length: int = 10, col: str = "close") -> pl.Series:
    """Rate of change in percent: ``100 * (x / x.shift(length) - 1)``.

    Args:
        panel: Long-format Panel.
        length: Lookback in bars.
        col: Source column.

    Returns:
        Series named ``roc``.
    """
    require_columns(panel, col)
    require_length(length)
    prev = shift(pl.col(col), length)
    return eval_series(panel, 100.0 * safe_div(pl.col(col) - prev, prev), "roc")


def mom(panel: pl.DataFrame, length: int = 10, col: str = "close") -> pl.Series:
    """Momentum: ``x - x.shift(length)``.

    Args:
        panel: Long-format Panel.
        length: Lookback in bars.
        col: Source column.

    Returns:
        Series named ``mom``.
    """
    require_columns(panel, col)
    require_length(length)
    return eval_series(panel, pl.col(col) - shift(pl.col(col), length), "mom")


def _rsi_expr(expr: pl.Expr, length: int) -> pl.Expr:
    delta = expr.diff().over(GROUP)
    gain = pl.when(delta.is_null()).then(None).when(delta > 0).then(delta).otherwise(0.0)
    loss = pl.when(delta.is_null()).then(None).when(delta < 0).then(-delta).otherwise(0.0)
    avg_gain = rma_expr(gain, length)
    avg_loss = rma_expr(loss, length)
    return (
        pl.when(avg_loss == 0)
        .then(pl.when(avg_gain == 0).then(50.0).otherwise(100.0))
        .otherwise(100.0 - 100.0 / (1.0 + avg_gain / avg_loss))
    )
