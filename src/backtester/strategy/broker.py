"""Broker: next-bar open fills, signed weights, class fees, rebalance band."""

from __future__ import annotations

import polars as pl

from backtester.strategy.types import BrokerConfig

_EPS = 1e-12
_TARGET_COLS = ("ts_utc", "instrument_id", "weight")


def execute(
    panel: pl.DataFrame,
    targets: pl.DataFrame,
    config: BrokerConfig,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Rebalance at next-bar open to the previous close's Target.

    ``weight.shift(1)`` is the fill clock (ADR-0004). A Target at ``t`` cannot
    change Fills at open ``t``. ``allow_short`` zeros negative weights. The
    rebalance band is hysteresis vs last held Target (not drifted). Cash is
    leftover when gross exposure is under 1. Equity compounds in Polars
    (``cum_prod`` of gap × bar − fee).

    Args:
        panel: Long OHLCV plus ``fee_bps`` (default 0).
        targets: ``ts_utc``, ``instrument_id``, ``weight``.
        config: Band, short gate, initial NAV.

    Returns:
        ``(fills, equity)``. Fills: ``ts_utc``, ``instrument_id``, ``size``,
        ``price``, ``fee``. Equity: one row per timestamp (close mark).
    """
    missing = [c for c in _TARGET_COLS if c not in targets.columns]
    if missing:
        raise ValueError(f"targets missing columns: {missing}")
    work = panel
    if "fee_bps" not in work.columns:
        work = work.with_columns(pl.lit(0.0).alias("fee_bps"))
    work = (
        work.join(
            targets.select(["ts_utc", "instrument_id", "weight"]),
            on=["ts_utc", "instrument_id"],
            how="left",
        )
        .sort(["instrument_id", "ts_utc"])
        .with_columns(pl.col("weight").cast(pl.Float64))
    )
    signed = pl.col("weight")
    if not config.allow_short:
        signed = pl.when(signed < 0.0).then(0.0).otherwise(signed)
    work = work.with_columns(signed.alias("weight"))
    desired = pl.col("weight").shift(1).over("instrument_id").fill_null(0.0)
    work = work.with_columns(desired.alias("desired_weight"))
    work = _apply_band(work, config.rebalance_band)
    prev_held = pl.col("held_weight").shift(1).over("instrument_id").fill_null(0.0)
    prev_close = pl.col("close").shift(1).over("instrument_id")
    gap_ret = (
        pl.when(prev_close.is_null() | (prev_close.abs() < _EPS)).then(0.0).otherwise(pl.col("open") / prev_close - 1.0)
    )
    bar_ret = pl.when(pl.col("open").abs() < _EPS).then(0.0).otherwise(pl.col("close") / pl.col("open") - 1.0)
    dw = pl.col("held_weight") - prev_held
    work = work.with_columns(
        prev_held.alias("prev_held"),
        gap_ret.alias("gap_ret"),
        bar_ret.alias("bar_ret"),
        dw.alias("dw"),
    )
    work = work.with_columns(
        (pl.col("prev_held") * pl.col("gap_ret")).alias("gap_contrib"),
        (pl.col("held_weight") * pl.col("bar_ret")).alias("bar_contrib"),
        (pl.col("dw").abs() * pl.col("fee_bps") / 10_000.0).alias("fee_frac_i"),
        pl.col("dw").abs().alias("turn_i"),
        pl.col("held_weight").abs().alias("abs_w"),
    )
    agg = (
        work.group_by("ts_utc")
        .agg(
            pl.col("gap_contrib").sum().alias("port_gap"),
            pl.col("bar_contrib").sum().alias("port_bar"),
            pl.col("fee_frac_i").sum().alias("fee_frac"),
            pl.col("turn_i").sum().alias("turnover"),
            pl.col("abs_w").sum().alias("gross"),
        )
        .sort("ts_utc")
        .with_columns(((1.0 + pl.col("port_gap")) * (1.0 + pl.col("port_bar") - pl.col("fee_frac")) - 1.0).alias("r"))
        .with_columns((pl.lit(config.initial_equity) * (1.0 + pl.col("r")).cum_prod()).alias("equity"))
        .with_columns(
            (pl.col("equity").shift(1).fill_null(config.initial_equity) * (1.0 + pl.col("port_gap"))).alias("nav_open"),
        )
        .with_columns(
            (pl.col("nav_open") * pl.col("fee_frac")).alias("fee"),
            (pl.col("equity") * (1.0 - pl.col("gross")).clip(lower_bound=0.0)).alias("cash"),
        )
    )
    with_nav = work.join(agg.select(["ts_utc", "nav_open"]), on="ts_utc", how="left")
    fills = (
        with_nav.filter(pl.col("dw").abs() > _EPS)
        .select(
            "ts_utc",
            "instrument_id",
            (pl.col("dw") * pl.col("nav_open") / pl.col("open")).alias("size"),
            pl.col("open").alias("price"),
            (pl.col("dw").abs() * pl.col("nav_open") * pl.col("fee_bps") / 10_000.0).alias("fee"),
        )
        .sort(["ts_utc", "instrument_id"])
    )
    equity = agg.select(
        "ts_utc",
        "equity",
        "cash",
        "gross",
        "turnover",
        "fee",
        "r",
        "nav_open",
    )
    return fills, equity


def _apply_band(frame: pl.DataFrame, band: float) -> pl.DataFrame:
    """Hysteresis: update held only when ``|desired − held| >= band``."""
    parts: list[pl.DataFrame] = []
    groups = frame.partition_by("instrument_id", maintain_order=True)
    for grp in groups:
        desired = grp.get_column("desired_weight").to_list()
        held: list[float] = []
        current = 0.0
        for raw in desired:
            val = 0.0 if raw is None else float(raw)
            if abs(val - current) >= band:
                current = val
            held.append(current)
        parts.append(grp.with_columns(pl.Series("held_weight", held, dtype=pl.Float64)))
    if not parts:
        return frame.with_columns(pl.lit(0.0).alias("held_weight"))
    return pl.concat(parts)
