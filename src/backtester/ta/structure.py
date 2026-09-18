"""Causal fractal swings and HH/HL/LH/LL labels."""

from __future__ import annotations

import polars as pl

from backtester.ta._core import (
    GROUP,
    eval_exprs,
    eval_series,
    group_idx,
    require_columns,
    require_length,
    shift,
)


def swing_high(panel: pl.DataFrame, left: int = 2, right: int = 2) -> pl.Series:
    """True on the bar that *confirms* a fractal swing high.

    A pivot at ``t - right`` is a swing high when its high is strictly greater
    than every high in ``[t - right - left, t]``. The flag is aligned to the
    confirmation bar so values do not depend on future rows.

    Args:
        panel: Long-format Panel with high.
        left: Bars before the pivot.
        right: Bars after the pivot required to confirm.

    Returns:
        Boolean Series named ``swing_high`` (null during warm-up).
    """
    require_columns(panel, "high")
    require_length(left, name="left")
    require_length(right, name="right")
    return eval_series(panel, _fractal("high", left, right, greater=True), "swing_high")


def swing_low(panel: pl.DataFrame, left: int = 2, right: int = 2) -> pl.Series:
    """True on the bar that *confirms* a fractal swing low.

    Args:
        panel: Long-format Panel with low.
        left: Bars before the pivot.
        right: Bars after the pivot required to confirm.

    Returns:
        Boolean Series named ``swing_low`` (null during warm-up).
    """
    require_columns(panel, "low")
    require_length(left, name="left")
    require_length(right, name="right")
    return eval_series(panel, _fractal("low", left, right, greater=False), "swing_low")


def swing_labels(panel: pl.DataFrame, left: int = 2, right: int = 2) -> pl.Series:
    """HH / HL / LH / LL (and H, L, EH, EL) on confirmation bars.

    Each confirmed fractal is labeled against the previous confirmed swing of
    the **same type** only. Past labels are never rewritten (no retroactive
    collapse of consecutive same-type swings). A bar that is both a high and a
    low is skipped.

    Args:
        panel: Long-format Panel with high/low.
        left: Bars before the pivot.
        right: Bars after the pivot required to confirm.

    Returns:
        String Series named ``swing_label`` (null on non-pivot bars).
    """
    require_columns(panel, "high", "low")
    require_length(left, name="left")
    require_length(right, name="right")
    is_high = _fractal("high", left, right, greater=True)
    is_low = _fractal("low", left, right, greater=False)
    both = is_high.fill_null(False) & is_low.fill_null(False)
    is_high = pl.when(both).then(False).otherwise(is_high)
    is_low = pl.when(both).then(False).otherwise(is_low)
    pivot_high = shift(pl.col("high"), right)
    pivot_low = shift(pl.col("low"), right)
    prev_high = pl.when(is_high.fill_null(False)).then(pivot_high).forward_fill().over(GROUP).shift(1).over(GROUP)
    prev_low = pl.when(is_low.fill_null(False)).then(pivot_low).forward_fill().over(GROUP).shift(1).over(GROUP)
    high_label = (
        pl.when(~is_high.fill_null(False))
        .then(None)
        .when(prev_high.is_null())
        .then(pl.lit("H"))
        .when(pivot_high > prev_high)
        .then(pl.lit("HH"))
        .when(pivot_high < prev_high)
        .then(pl.lit("LH"))
        .otherwise(pl.lit("EH"))
    )
    low_label = (
        pl.when(~is_low.fill_null(False))
        .then(None)
        .when(prev_low.is_null())
        .then(pl.lit("L"))
        .when(pivot_low > prev_low)
        .then(pl.lit("HL"))
        .when(pivot_low < prev_low)
        .then(pl.lit("LL"))
        .otherwise(pl.lit("EL"))
    )
    label = pl.when(is_high.fill_null(False)).then(high_label).when(is_low.fill_null(False)).then(low_label)
    return eval_series(panel, label, "swing_label")


def swings(panel: pl.DataFrame, left: int = 2, right: int = 2) -> pl.DataFrame:
    """Swing high, swing low, and structure labels together.

    Args:
        panel: Long-format Panel with high/low.
        left: Bars before the pivot.
        right: Bars after the pivot required to confirm.

    Returns:
        Columns ``swing_high``, ``swing_low``, ``swing_label``.
    """
    require_columns(panel, "high", "low")
    require_length(left, name="left")
    require_length(right, name="right")
    is_high = _fractal("high", left, right, greater=True)
    is_low = _fractal("low", left, right, greater=False)
    both = is_high.fill_null(False) & is_low.fill_null(False)
    is_high_f = pl.when(both).then(False).otherwise(is_high)
    is_low_f = pl.when(both).then(False).otherwise(is_low)
    pivot_high = shift(pl.col("high"), right)
    pivot_low = shift(pl.col("low"), right)
    prev_high = pl.when(is_high_f.fill_null(False)).then(pivot_high).forward_fill().over(GROUP).shift(1).over(GROUP)
    prev_low = pl.when(is_low_f.fill_null(False)).then(pivot_low).forward_fill().over(GROUP).shift(1).over(GROUP)
    high_label = (
        pl.when(~is_high_f.fill_null(False))
        .then(None)
        .when(prev_high.is_null())
        .then(pl.lit("H"))
        .when(pivot_high > prev_high)
        .then(pl.lit("HH"))
        .when(pivot_high < prev_high)
        .then(pl.lit("LH"))
        .otherwise(pl.lit("EH"))
    )
    low_label = (
        pl.when(~is_low_f.fill_null(False))
        .then(None)
        .when(prev_low.is_null())
        .then(pl.lit("L"))
        .when(pivot_low > prev_low)
        .then(pl.lit("HL"))
        .when(pivot_low < prev_low)
        .then(pl.lit("LL"))
        .otherwise(pl.lit("EL"))
    )
    label = pl.when(is_high_f.fill_null(False)).then(high_label).when(is_low_f.fill_null(False)).then(low_label)
    return eval_exprs(
        panel,
        {
            "swing_high": is_high_f,
            "swing_low": is_low_f,
            "swing_label": label,
        },
    )


def _fractal(col: str, left: int, right: int, *, greater: bool) -> pl.Expr:
    pivot = shift(pl.col(col), right)
    parts: list[pl.Expr] = []
    for k in range(1, left + 1):
        neighbor = shift(pl.col(col), right + k)
        parts.append(pivot > neighbor if greater else pivot < neighbor)
    for k in range(right):
        neighbor = shift(pl.col(col), k)
        parts.append(pivot > neighbor if greater else pivot < neighbor)
    folded = parts[0]
    for part in parts[1:]:
        folded = folded & part
    enough = group_idx() >= (left + right)
    return pl.when(enough).then(folded).otherwise(None)
