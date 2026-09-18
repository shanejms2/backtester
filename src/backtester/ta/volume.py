"""Volume Indicators: OBV, MFI, CMF, session VWAP."""

from __future__ import annotations

import polars as pl

from backtester.ta._core import (
    _ROW,
    GROUP,
    eval_series,
    require_columns,
    require_length,
    rolling_sum,
    safe_div,
    session_day_expr,
    typical_price,
    with_sorted_index,
)


def obv(panel: pl.DataFrame) -> pl.Series:
    """On-Balance Volume. First bar is 0; then signed volume is accumulated.

    Args:
        panel: Long-format Panel with close and volume.

    Returns:
        Series named ``obv``.
    """
    require_columns(panel, "close", "volume")
    chg = pl.col("close").diff().over(GROUP)
    signed = pl.when(chg > 0).then(pl.col("volume")).when(chg < 0).then(-pl.col("volume")).otherwise(0.0)
    return eval_series(panel, signed.cum_sum().over(GROUP), "obv")


def mfi(panel: pl.DataFrame, length: int = 14) -> pl.Series:
    """Money Flow Index (volume-weighted RSI of typical price).

    Args:
        panel: Long-format Panel with OHLCV.
        length: Lookback in bars.

    Returns:
        Series named ``mfi``.
    """
    require_columns(panel, "high", "low", "close", "volume")
    require_length(length)
    tp = typical_price()
    money_flow = tp * pl.col("volume")
    delta = tp.diff().over(GROUP)
    pos = pl.when(delta.is_null()).then(None).when(delta > 0).then(money_flow).otherwise(0.0)
    neg = pl.when(delta.is_null()).then(None).when(delta < 0).then(money_flow).otherwise(0.0)
    pos_sum = rolling_sum(pos, length)
    neg_sum = rolling_sum(neg, length)
    ratio = safe_div(pos_sum, neg_sum)
    expr = (
        pl.when(neg_sum == 0)
        .then(pl.when(pos_sum == 0).then(None).otherwise(100.0))
        .otherwise(100.0 - 100.0 / (1.0 + ratio))
    )
    return eval_series(panel, expr, "mfi")


def cmf(panel: pl.DataFrame, length: int = 20) -> pl.Series:
    """Chaikin Money Flow.

    Args:
        panel: Long-format Panel with OHLCV.
        length: Summation window.

    Returns:
        Series named ``cmf``.
    """
    require_columns(panel, "high", "low", "close", "volume")
    require_length(length)
    hl = pl.col("high") - pl.col("low")
    mfm = pl.when(hl == 0).then(0.0).otherwise((2.0 * pl.col("close") - pl.col("high") - pl.col("low")) / hl)
    mfv = mfm * pl.col("volume")
    return eval_series(panel, safe_div(rolling_sum(mfv, length), rolling_sum(pl.col("volume"), length)), "cmf")


def vwap(panel: pl.DataFrame) -> pl.Series:
    """Session VWAP of typical price, reset on the Instrument calendar day.

    Uses ``session_date`` if present, else a date-typed / date-like / IANA
    ``calendar`` column, else the UTC date of ``ts_utc``. Cumulative from the
    session open through the current bar (causal).

    Args:
        panel: Long-format Panel with OHLCV and ``ts_utc``.

    Returns:
        Series named ``vwap``.
    """
    require_columns(panel, "high", "low", "close", "volume")
    day = session_day_expr(panel)
    numbered = with_sorted_index(panel).with_columns(day.alias("_session_day"))
    tp = typical_price()
    pv = tp * pl.col("volume")
    out = numbered.with_columns(
        safe_div(
            pv.cum_sum().over(GROUP, "_session_day"),
            pl.col("volume").cum_sum().over(GROUP, "_session_day"),
        ).alias("vwap")
    )
    return out.sort(_ROW)["vwap"]
