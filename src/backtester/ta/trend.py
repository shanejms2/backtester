"""Trend Indicators: SMA, EMA, WMA, MACD, ADX, Supertrend, Aroon."""

from __future__ import annotations

import polars as pl

from backtester.ta._core import (
    _ROW,
    GROUP,
    ema_expr,
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
    true_range,
    with_sorted_index,
)


def sma(panel: pl.DataFrame, length: int = 20, col: str = "close") -> pl.Series:
    """Simple moving average of ``col``. First ``length - 1`` bars are null.

    Args:
        panel: Long-format Panel.
        length: Window in bars.
        col: Source column.

    Returns:
        Series named ``sma``, aligned to ``panel`` rows.
    """
    require_columns(panel, col)
    require_length(length)
    return eval_series(panel, rolling_mean(pl.col(col), length), "sma")


def ema(panel: pl.DataFrame, length: int = 20, col: str = "close") -> pl.Series:
    """Exponential moving average with an SMA seed.

    ``alpha = 2 / (length + 1)``. The first non-null value is the SMA of the
    first ``length`` observations; earlier bars are null.

    Args:
        panel: Long-format Panel.
        length: Span (and seed window).
        col: Source column.

    Returns:
        Series named ``ema``, aligned to ``panel`` rows.
    """
    require_columns(panel, col)
    require_length(length)
    return eval_series(panel, ema_expr(pl.col(col), length), "ema")


def wma(panel: pl.DataFrame, length: int = 20, col: str = "close") -> pl.Series:
    """Weighted moving average; oldest bar weight 1, newest weight ``length``.

    Args:
        panel: Long-format Panel.
        length: Window in bars.
        col: Source column.

    Returns:
        Series named ``wma``, aligned to ``panel`` rows.
    """
    require_columns(panel, col)
    require_length(length)
    weights = [float(i) for i in range(1, length + 1)]
    expr = pl.col(col).rolling_mean(window_size=length, weights=weights, min_samples=length).over(GROUP)
    return eval_series(panel, expr, "wma")


def macd(
    panel: pl.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
    col: str = "close",
) -> pl.DataFrame:
    """MACD line, signal, and histogram.

    Line is ``EMA(fast) − EMA(slow)``. Signal is an SMA-seeded EMA of the
    line. Histogram is line − signal.

    Args:
        panel: Long-format Panel.
        fast: Fast EMA length.
        slow: Slow EMA length.
        signal: Signal EMA length.
        col: Source column.

    Returns:
        Columns ``macd``, ``signal``, ``hist``.

    Raises:
        ValueError: ``slow`` is not greater than ``fast``.
    """
    require_columns(panel, col)
    require_length(fast, name="fast")
    require_length(slow, name="slow")
    require_length(signal, name="signal")
    if slow <= fast:
        raise ValueError(f"slow must be > fast, got slow={slow}, fast={fast}")
    line = ema_expr(pl.col(col), fast) - ema_expr(pl.col(col), slow)
    sig = ema_expr(line, signal)
    return eval_exprs(
        panel,
        {
            "macd": line,
            "signal": sig,
            "hist": line - sig,
        },
    )


def adx(panel: pl.DataFrame, length: int = 14) -> pl.DataFrame:
    """Average Directional Index with +DI and −DI (Wilder smoothing).

    Args:
        panel: Long-format Panel with OHLC.
        length: Wilder period.

    Returns:
        Columns ``adx``, ``plus_di``, ``minus_di``.
    """
    require_columns(panel, "high", "low", "close")
    require_length(length)
    up = pl.col("high") - shift(pl.col("high"))
    down = shift(pl.col("low")) - pl.col("low")
    plus_dm = pl.when(up.is_null()).then(None).when((up > down) & (up > 0)).then(up).otherwise(0.0)
    minus_dm = pl.when(down.is_null()).then(None).when((down > up) & (down > 0)).then(down).otherwise(0.0)
    atr = rma_expr(true_range(), length)
    plus_di = 100.0 * safe_div(rma_expr(plus_dm, length), atr)
    minus_di = 100.0 * safe_div(rma_expr(minus_dm, length), atr)
    dx = 100.0 * safe_div((plus_di - minus_di).abs(), plus_di + minus_di)
    return eval_exprs(
        panel,
        {
            "adx": rma_expr(dx, length),
            "plus_di": plus_di,
            "minus_di": minus_di,
        },
    )


def supertrend(panel: pl.DataFrame, length: int = 10, multiplier: float = 3.0) -> pl.DataFrame:
    """Supertrend line and direction.

    Bands are ``HL2 ± multiplier * ATR(length)`` with the usual close-based
    ratchet. Direction is ``+1`` (close above the line) or ``-1``. The ratchet
    is sequential per Instrument; ATR itself is vectorized.

    Args:
        panel: Long-format Panel with OHLC.
        length: ATR period.
        multiplier: Band width in ATRs.

    Returns:
        Columns ``supertrend``, ``direction``.
    """
    require_columns(panel, "high", "low", "close")
    require_length(length)
    if multiplier <= 0:
        raise ValueError(f"multiplier must be > 0, got {multiplier}")
    numbered = with_sorted_index(panel).with_columns(rma_expr(true_range(), length).alias("_atr"))

    def _one(group: pl.DataFrame) -> pl.DataFrame:
        st, direction = _ratchet_supertrend(
            group["high"].to_list(),
            group["low"].to_list(),
            group["close"].to_list(),
            group["_atr"].to_list(),
            multiplier,
        )
        return pl.DataFrame(
            {
                _ROW: group[_ROW],
                "supertrend": pl.Series("supertrend", st, dtype=pl.Float64),
                "direction": pl.Series("direction", direction, dtype=pl.Float64),
            }
        )

    out = numbered.group_by(GROUP, maintain_order=True).map_groups(_one)
    return out.sort(_ROW).select(["supertrend", "direction"])


def aroon(panel: pl.DataFrame, length: int = 25) -> pl.DataFrame:
    """Aroon up / down over a ``length``-bar window (current bar included).

    100 when the extreme is the current bar. Ties keep the most recent extreme.

    Args:
        panel: Long-format Panel with high/low.
        length: Lookback in bars.

    Returns:
        Columns ``aroon_up``, ``aroon_down``.
    """
    require_columns(panel, "high", "low")
    require_length(length)
    up = 100.0 * (length - _bars_since_extreme(pl.col("high"), length, mode="max")) / length
    down = 100.0 * (length - _bars_since_extreme(pl.col("low"), length, mode="min")) / length
    return eval_exprs(panel, {"aroon_up": up, "aroon_down": down})


def _bars_since_extreme(expr: pl.Expr, length: int, *, mode: str) -> pl.Expr:
    window = rolling_max(expr, length) if mode == "max" else rolling_min(expr, length)
    matches = [pl.when(shift(expr, k) == window).then(pl.lit(k)) for k in range(length)]
    return pl.coalesce(matches)


def _ratchet_supertrend(
    high: list[float | None],
    low: list[float | None],
    close: list[float | None],
    atr: list[float | None],
    multiplier: float,
) -> tuple[list[float | None], list[float | None]]:
    """Ratchet Supertrend bands. Sequential by construction."""
    n = len(close)
    st: list[float | None] = [None] * n
    direction: list[float | None] = [None] * n
    final_upper: list[float | None] = [None] * n
    final_lower: list[float | None] = [None] * n
    prev: int | None = None
    for i in range(n):
        h, lo, c, a = high[i], low[i], close[i], atr[i]
        if h is None or lo is None or c is None or a is None:
            continue
        hl2 = (h + lo) / 2.0
        basic_upper = hl2 + multiplier * a
        basic_lower = hl2 - multiplier * a
        if prev is None:
            final_upper[i] = basic_upper
            final_lower[i] = basic_lower
            if c <= basic_upper:
                st[i] = basic_upper
                direction[i] = -1.0
            else:
                st[i] = basic_lower
                direction[i] = 1.0
            prev = i
            continue
        p_upper, p_lower, p_close, p_dir = final_upper[prev], final_lower[prev], close[prev], direction[prev]
        if p_lower is not None and (basic_lower > p_lower or (p_close is not None and p_close < p_lower)):
            final_lower[i] = basic_lower
        else:
            final_lower[i] = p_lower
        if p_upper is not None and (basic_upper < p_upper or (p_close is not None and p_close > p_upper)):
            final_upper[i] = basic_upper
        else:
            final_upper[i] = p_upper
        fu, fl = final_upper[i], final_lower[i]
        if p_dir == -1.0:
            if fu is not None and c > fu:
                direction[i] = 1.0
                st[i] = fl
            else:
                direction[i] = -1.0
                st[i] = fu
        elif fl is not None and c < fl:
            direction[i] = -1.0
            st[i] = fu
        else:
            direction[i] = 1.0
            st[i] = fl
        prev = i
    return st, direction
