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
    change Fills at open ``t``.     ``allow_short`` zeros negative weights. The
    rebalance band is hysteresis vs last held Target. A **null** Target holds
    the last weight; with ``drift`` that weight follows price until the next
    Target. A target of **0** fully exits (even with ``half_move``). Cash is
    leftover when gross exposure is under 1, and earns
    ``cash_weight * daily_tbill``. ``daily_tbill`` is the period T-bill return
    for the gap ending at this stamp (IRX percent / 100 / 365 × calendar days
    since the previous panel row). Equity compounds in Polars
    (``cum_prod`` of gap × bar − fee + cash).

    Args:
        panel: Long OHLCV plus ``fee_bps`` (default 0) and optional
            ``daily_tbill``.
        targets: ``ts_utc``, ``instrument_id``, ``weight``.
        config: Band, short gate, initial NAV, optional half-move.

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
    if "daily_tbill" not in work.columns:
        work = work.with_columns(pl.lit(0.0).alias("daily_tbill"))
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
    desired = pl.col("weight").shift(1).over("instrument_id")
    work = work.with_columns(desired.alias("desired_weight"))
    if config.drift:
        work = _drift_held(work, config)
    else:
        work = _apply_band(work, config)
        work = work.with_columns(pl.col("held_weight").shift(1).over("instrument_id").fill_null(0.0).alias("prev_held"))
    last_close = pl.col("close").forward_fill().over("instrument_id")
    prev_close = last_close.shift(1).over("instrument_id")
    closed = pl.col("open").is_null() | pl.col("close").is_null()
    gap_ret = (
        pl.when(closed | prev_close.is_null() | (prev_close.abs() < _EPS))
        .then(0.0)
        .otherwise(pl.col("open") / prev_close - 1.0)
    )
    bar_ret = (
        pl.when(closed | (pl.col("open").abs() < _EPS)).then(0.0).otherwise(pl.col("close") / pl.col("open") - 1.0)
    )
    dw = pl.when(closed).then(0.0).otherwise(pl.col("held_weight") - pl.col("prev_held"))
    work = work.with_columns(
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
            pl.col("daily_tbill").first().alias("daily_tbill"),
        )
        .sort("ts_utc")
        .with_columns((1.0 - pl.col("gross")).clip(lower_bound=0.0).alias("cash_weight"))
        .with_columns(
            (
                ((1.0 + pl.col("port_gap")) * (1.0 + pl.col("port_bar") - pl.col("fee_frac")) - 1.0)
                + pl.col("cash_weight") * pl.col("daily_tbill")
            ).alias("r")
        )
        .with_columns((pl.lit(config.initial_equity) * (1.0 + pl.col("r")).cum_prod()).alias("equity"))
        .with_columns(
            (pl.col("equity").shift(1).fill_null(config.initial_equity) * (1.0 + pl.col("port_gap"))).alias("nav_open"),
        )
        .with_columns(
            (pl.col("nav_open") * pl.col("fee_frac")).alias("fee"),
            (pl.col("equity") * pl.col("cash_weight")).alias("cash"),
        )
    )
    with_nav = work.join(agg.select(["ts_utc", "nav_open"]), on="ts_utc", how="left")
    fills = (
        with_nav.filter((pl.col("dw").abs() > _EPS) & pl.col("open").is_not_null() & (pl.col("open").abs() > _EPS))
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
        "cash_weight",
        "daily_tbill",
    )
    return fills, equity


def _next_held(current: float, raw: float | None, px: float | None, config: BrokerConfig) -> float:
    """Band vs ``current``. Null desired or a closed bar holds. Zero fully exits."""
    if px is None or raw is None:
        return current
    val = float(raw)
    if abs(val) <= _EPS:
        return 0.0
    use_prop = config.band_floor > 0.0 or config.band_frac > 0.0
    thresh = max(config.band_floor, config.band_frac * abs(val)) if use_prop else config.rebalance_band
    if abs(val - current) >= thresh - _EPS:
        return current + 0.5 * (val - current) if config.half_move else val
    return current


def _apply_band(frame: pl.DataFrame, config: BrokerConfig) -> pl.DataFrame:
    """Hysteresis vs last held Target; null desired holds; 0 fully exits."""
    parts: list[pl.DataFrame] = []
    groups = frame.partition_by("instrument_id", maintain_order=True)
    for grp in groups:
        desired = grp.get_column("desired_weight").to_list()
        opens = grp.get_column("open").to_list()
        held: list[float] = []
        current = 0.0
        for raw, px in zip(desired, opens, strict=True):
            current = _next_held(
                current, None if raw is None else float(raw), None if px is None else float(px), config
            )
            held.append(current)
        parts.append(grp.with_columns(pl.Series("held_weight", held, dtype=pl.Float64)))
    if not parts:
        return frame.with_columns(pl.lit(0.0).alias("held_weight"))
    return pl.concat(parts)


def _drift_held(frame: pl.DataFrame, config: BrokerConfig) -> pl.DataFrame:
    """Let weights follow price between Targets. The next Target resets them.

    Dollars, not the prior weight, are the state. Each timestamp:

    1. Entering weight is yesterday's dollars over NAV, before today's gap.
    2. The band turns the desired Target into a post-trade weight. Null holds.
       Zero exits. A closed bar holds.
    3. Overnight gap (prior close → this open) is applied to entering dollars.
    4. Dollars are reset to the post-band weights at the post-gap NAV. The
       fee scales that NAV down.
    5. Open-to-close return marks the new book. Leftover cash earns
       ``daily_tbill``.

    The next session's entering weight is those drifted dollars. A fresh
    non-null Target is what pulls the book back to the Spec weights.
    """
    if frame.is_empty():
        return frame.with_columns(
            pl.lit(0.0).alias("held_weight"),
            pl.lit(0.0).alias("prev_held"),
        )
    ordered = frame.sort(["ts_utc", "instrument_id"]).with_row_index("_i")
    prev_held = [0.0] * ordered.height
    held = [0.0] * ordered.height
    dollars: dict[str, float] = {}
    cash = 1.0
    last_close: dict[str, float] = {}
    for ts_value, grp in ordered.group_by("ts_utc", maintain_order=True):
        del ts_value
        rows = list(grp.iter_rows(named=True))
        # 1. Entering weight is yesterday's dollars over NAV, before today's gap.
        nav = cash + sum(dollars.values())
        if nav <= _EPS:
            nav = 1.0
        pres: dict[str, float] = {}
        posts: dict[str, float] = {}
        gaps: dict[str, float] = {}
        bars: dict[str, float] = {}
        fees: dict[str, float] = {}
        for row in rows:
            iid = str(row["instrument_id"])
            pre = dollars.get(iid, 0.0) / nav
            px = row["open"]
            close = row["close"]
            raw = row["desired_weight"]
            # 2. Band: null holds, zero exits, a closed bar holds.
            post = _next_held(
                pre,
                None if raw is None else float(raw),
                None if px is None else float(px),
                config,
            )
            prev_px = last_close.get(iid)
            if px is None or close is None or prev_px is None or abs(prev_px) <= _EPS:
                gap = 0.0
            else:
                gap = float(px) / prev_px - 1.0
            bar = 0.0 if px is None or close is None or abs(float(px)) <= _EPS else float(close) / float(px) - 1.0
            pres[iid] = pre
            posts[iid] = post
            gaps[iid] = gap
            bars[iid] = bar
            fees[iid] = float(row["fee_bps"] or 0.0)
            idx = int(row["_i"])
            prev_held[idx] = pre
            held[idx] = post
            if close is not None:
                last_close[iid] = float(close)
        # 3. Overnight gap on the entering dollars.
        for iid, pre in pres.items():
            dollars[iid] = pre * nav * (1.0 + gaps[iid])
        # 4. Reset dollars onto the post-band book, then charge the fee.
        nav_gap = cash + sum(dollars.values())
        gross = sum(abs(w) for w in posts.values())
        dollars = {iid: posts[iid] * nav_gap for iid in posts}
        cash = max(0.0, 1.0 - gross) * nav_gap
        fee_frac = sum(abs(posts[iid] - pres[iid]) * fees[iid] / 10_000.0 for iid in posts)
        nav_after = nav_gap * (1.0 - fee_frac)
        booked = cash + sum(dollars.values())
        if booked > _EPS:
            scale = nav_after / booked
            dollars = {iid: value * scale for iid, value in dollars.items()}
            cash *= scale
        # 5. Open-to-close on the new book; T-bill yield on leftover cash.
        tbill = float(rows[0]["daily_tbill"] or 0.0)
        for iid in list(dollars):
            dollars[iid] *= 1.0 + bars.get(iid, 0.0)
        cash *= 1.0 + tbill
    return ordered.with_columns(
        pl.Series("prev_held", prev_held, dtype=pl.Float64),
        pl.Series("held_weight", held, dtype=pl.Float64),
    ).drop("_i")
