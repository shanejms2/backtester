"""prism: calendar TSMOM, EWMA vol, hysteresis, monthly clusters, 12% vol target.

One session of ``Prism.targets`` does the following, in order. The Broker
still fills at the next open; this module only emits weights.

1. Price is ``adj_close`` when present, else ``close``. Per-name return,
   fast/slow EWMA vol, and dollar ADV come from that price.
2. Calendar lookbacks (1/3/6/12 months by default) become a raw TSMOM score.
   Hysteresis latches that score ON above ``+band`` (clipped to 0..1) and OFF
   below ``−band``. Inside the band the previous state is held.
3. A name is live only when it printed, is not stale, has enough history,
   and clears the ADV and ``min_adv`` gates.
4. Phase-in / phase-out ramps move a name toward 1 or 0 across sessions
   instead of jumping the book in one day.
5. Once a calendar month, live names are clustered on a shrunk correlation
   matrix (Ledoit–Wolf over ``lw_window``). Clusters stay fixed until the
   month changes.
6. Each session, inverse-vol baskets inside those clusters receive a budget
   from trend / cluster-vol, then per-name caps (cluster cap, ref-vol,
   stock and inverse-ETF haircut, liquidity). Water-fill respects the caps.
7. ``allow_cash`` (default) scales the book to ``vol_target`` and leaves the
   rest in T-bills. ``allow_cash: false`` rescales ON names to 100% and, when
   every trend is off, holds yesterday's book.
8. A name that did not print gets a null weight (Broker holds). Weight 0 is
   a full exit. ``diagnostics: true`` writes parquet and csv under ``$TMPDIR``.
"""

from __future__ import annotations

import math
import os
import tempfile
from calendar import monthrange
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field, field_validator
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import squareform

from backtester.strategy.interface import Strategy
from backtester.strategy.registry import register
from backtester.ta import ewm_std

_NY = ZoneInfo("America/New_York")
_YEAR_DAYS = 365.25
_MIN_CORR_OBS = 20
_DIAG_PATH = "prism-diagnostics"
_EPS = 1e-15

FloatArr = NDArray[np.float64]
IntArr = NDArray[np.int32]
BoolArr = NDArray[np.bool_]


class PrismParams(BaseModel):
    """Writeup defaults. Unknown keys fail fast.

    ``allow_cash`` (default True) leaves leftover weight in T-bills after the
    12% vol target and trend-off names. False rescales ON names to 100% and
    holds yesterday's book when every trend is off.
    """

    model_config = ConfigDict(extra="forbid")

    vol_target: float = 0.12
    corr_cut: float = 0.60
    hysteresis: float = 0.10
    ref_vol: float = 0.25
    lookbacks: list[int] = Field(default_factory=lambda: [1, 3, 6, 12])
    ewma_hl_fast: float = 10.0
    ewma_hl_slow: float = 60.0
    adv_window: int = 20
    adv_max_pct: float = 0.01
    phase_in: int = 21
    phase_out: int = 5
    stale_sessions: int = 5
    cluster_cap_floor: float = 0.30
    cluster_cap_num: float = 1.20
    stock_haircut: float = 0.50
    lw_window: int = 252
    lw_seed: int = 60
    nav_usd: float = 100_000.0
    min_liq_weight: float = 0.005
    allow_short: bool = False
    allow_cash: bool = True
    diagnostics: bool = False

    @field_validator(
        "vol_target",
        "corr_cut",
        "ref_vol",
        "ewma_hl_fast",
        "ewma_hl_slow",
        "adv_max_pct",
        "cluster_cap_floor",
        "cluster_cap_num",
        "stock_haircut",
        "nav_usd",
        "min_liq_weight",
    )
    @classmethod
    def _positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("must be > 0")
        return value

    @field_validator("hysteresis")
    @classmethod
    def _hysteresis_nonneg(cls, value: float) -> float:
        if value < 0:
            raise ValueError("hysteresis must be >= 0")
        return value

    @field_validator("adv_window", "phase_in", "phase_out", "stale_sessions", "lw_window", "lw_seed")
    @classmethod
    def _positive_int(cls, value: int) -> int:
        if value < 1:
            raise ValueError("must be >= 1")
        return value

    @field_validator("lookbacks")
    @classmethod
    def _lookbacks_ok(cls, value: list[int]) -> list[int]:
        if not value or any(m < 1 for m in value):
            raise ValueError("lookbacks must be positive calendar months")
        return list(value)

    @field_validator("corr_cut")
    @classmethod
    def _corr_cut_range(cls, value: float) -> float:
        if value > 1:
            raise ValueError("corr_cut must be <= 1")
        return value


def ledoit_wolf(returns: FloatArr) -> tuple[FloatArr, float]:
    """Constant-correlation Ledoit–Wolf shrinkage of a T×N return matrix.

    Identity shrinkage treats duplicate trackers as independent. The target
    here is constant correlation so copies keep asset vol and a shared ρ.

    Args:
        returns: Synchronized decision-clock returns, shape ``(T, N)``.

    Returns:
        ``(cov, shrinkage)``. ``cov`` is N×N. Shrinkage is in ``[0, 1]``.

    Raises:
        ValueError: Fewer than two observations or zero names.
    """
    x_raw = np.asarray(returns, dtype=np.float64)
    if x_raw.ndim != 2 or x_raw.shape[0] < 2 or x_raw.shape[1] < 1:
        raise ValueError("ledoit_wolf needs a T×N matrix with T >= 2 and N >= 1")
    t_obs, n_names = x_raw.shape
    centered = x_raw - x_raw.mean(axis=0, keepdims=True)
    sample = (centered.T @ centered) / t_obs
    if n_names == 1:
        return sample, 0.0
    std = np.sqrt(np.clip(np.diag(sample), 0.0, None))
    pair_corr = 0.0
    n_pairs = 0
    for i in range(n_names):
        for j in range(i + 1, n_names):
            if std[i] > 0 and std[j] > 0:
                pair_corr += sample[i, j] / (std[i] * std[j])
                n_pairs += 1
    r_bar = pair_corr / n_pairs if n_pairs else 0.0
    target = np.diag(np.diag(sample).copy())
    for i in range(n_names):
        for j in range(i + 1, n_names):
            target[i, j] = target[j, i] = r_bar * std[i] * std[j]
    delta = 0.0
    for t in range(t_obs):
        outer = np.outer(centered[t], centered[t])
        diff = outer - sample
        delta += float(np.sum(diff * diff))
    delta /= t_obs
    beta = float(np.sum((sample - target) ** 2))
    if beta <= 1e-18:
        return sample, 0.0
    shrinkage = min(delta / (t_obs * beta), 1.0)
    cov = (1.0 - shrinkage) * sample + shrinkage * target
    return cov, shrinkage


def cluster_labels(corr: FloatArr, cut: float) -> IntArr:
    """Average-linkage labels. Merge while distance ``1 − corr`` is at most ``1 − cut``.

    Args:
        corr: Square correlation matrix for the live names that day.
        cut: Correlation threshold (writeup default 0.60).

    Returns:
        1-based cluster ids, length ``corr.shape[0]``.
    """
    rho = np.asarray(corr, dtype=np.float64)
    n_names = rho.shape[0]
    if n_names == 0:
        return np.array([], dtype=np.int32)
    if n_names == 1:
        return np.array([1], dtype=np.int32)
    dist = np.clip(1.0 - rho, 0.0, 2.0)
    np.fill_diagonal(dist, 0.0)
    condensed = squareform(dist, checks=False)
    tree = linkage(condensed, method="average")
    labels = np.asarray(fcluster(tree, t=1.0 - cut, criterion="distance"), dtype=np.int32)
    return labels


def apply_hysteresis(raw: FloatArr, band: float) -> FloatArr:
    """Latch trend ON above ``+band`` and OFF below ``−band``.

    Inside ``±band`` the previous state is held. ON emits ``clip(raw, 0, 1)``;
    OFF emits 0. Null raw resets to 0 (not a flip through the band).
    """
    series = np.asarray(raw, dtype=np.float64)
    trend = np.zeros(series.shape[0], dtype=np.float64)
    prev = 0.0
    for i, value in enumerate(series):
        if not np.isfinite(value):
            state = 0.0
        elif value > band:
            state = min(1.0, max(0.0, float(value)))
        elif value < -band:
            state = 0.0
        else:
            state = prev
        trend[i] = state
        prev = state
    return trend


def _add_months(day: date, months: int) -> date:
    month_index = day.month + months
    year = day.year
    while month_index < 1:
        month_index += 12
        year -= 1
    while month_index > 12:
        month_index -= 12
        year += 1
    return date(year, month_index, min(day.day, monthrange(year, month_index)[1]))


def _calendar_raw(days: list[date], px: FloatArr, vol_ann: FloatArr, lookbacks: list[int]) -> FloatArr:
    n = len(days)
    raw = np.full(n, np.nan, dtype=np.float64)
    lags = dict.fromkeys(lookbacks, 0)
    for i in range(n):
        vol = vol_ann[i]
        if not np.isfinite(vol) or vol <= 0 or not np.isfinite(px[i]) or px[i] <= 0:
            continue
        sleeves: list[float] = []
        ok = True
        for months in lookbacks:
            target = _add_months(days[i], -months)
            lag = lags[months]
            while lag + 1 < i and days[lag + 1] <= target:
                lag += 1
            lags[months] = lag
            if lag >= i or days[lag] > target or not np.isfinite(px[lag]) or px[lag] <= 0:
                ok = False
                break
            tau = max((days[i] - days[lag]).days / _YEAR_DAYS, 1.0 / _YEAR_DAYS)
            mom = (px[i] / px[lag] - 1.0) / (vol * math.sqrt(tau))
            sleeves.append(min(1.0, max(-1.0, mom / 2.0)))
        if ok and len(sleeves) == len(lookbacks):
            raw[i] = sum(sleeves) / len(sleeves)
    return raw


def _pearson(xs: FloatArr, ys: FloatArr) -> float:
    if xs.size < _MIN_CORR_OBS:
        return 0.0
    dx = xs - xs.mean()
    dy = ys - ys.mean()
    vxx = float(dx @ dx)
    vyy = float(dy @ dy)
    if vxx <= 0 or vyy <= 0:
        return 0.0
    return float(dx @ dy) / math.sqrt(vxx * vyy)


def _corr_matrix(rets: FloatArr, live: list[int]) -> FloatArr:
    m = len(live)
    rho = np.eye(m, dtype=np.float64)
    for a in range(m):
        for b in range(a + 1, m):
            xa = rets[:, live[a]]
            yb = rets[:, live[b]]
            mask = np.isfinite(xa) & np.isfinite(yb)
            rho[a, b] = rho[b, a] = _pearson(xa[mask], yb[mask])
    return rho


def _port_vol(weights: FloatArr, cov: FloatArr, bpy: float) -> float:
    var = float(weights @ cov @ weights)
    return math.sqrt(max(var, 0.0)) * math.sqrt(max(bpy, 1.0))


def _waterfill(
    weights: FloatArr,
    asset_caps: FloatArr,
    cluster_id: list[int],
    clusters: list[list[int]],
    cluster_cap: float,
) -> FloatArr:
    """Clip to asset and cluster caps; leftover goes in-cluster first."""
    x = np.maximum(weights, 0.0)
    n_names = x.size
    locked_a = np.zeros(n_names, dtype=np.bool_)
    locked_c = np.zeros(len(clusters), dtype=np.bool_)

    def clip_once() -> float:
        excess = 0.0
        over = x > asset_caps + _EPS
        if np.any(over):
            excess += float(np.sum(x[over] - asset_caps[over]))
            x[over] = asset_caps[over]
            locked_a[over] = True
        for cid, members in enumerate(clusters):
            if not members:
                continue
            total = float(np.sum(x[members]))
            if total > cluster_cap + _EPS:
                excess += total - cluster_cap
                x[members] *= cluster_cap / total
                locked_c[cid] = True
        return excess

    for _ in range(n_names + len(clusters) + 6):
        leftover = clip_once()
        if leftover <= _EPS:
            break
        rec: list[int] = []
        rooms: list[float] = []
        for j in range(n_names):
            cid = cluster_id[j]
            if cid < 0 or locked_a[j] or (cid < len(locked_c) and locked_c[cid]):
                continue
            rooms.append(max(0.0, float(asset_caps[j] - x[j])))
            rec.append(j)
        sroom = sum(rooms)
        if sroom <= _EPS:
            rec, rooms = [], []
            for j in range(n_names):
                cid = cluster_id[j]
                if cid < 0 or locked_a[j]:
                    continue
                rooms.append(max(0.0, float(asset_caps[j] - x[j])))
                rec.append(j)
            sroom = sum(rooms)
        if sroom <= _EPS:
            break
        take = min(leftover, sroom)
        for j, room in zip(rec, rooms, strict=True):
            x[j] += take * (room / sroom)
    clip_once()
    return np.asarray(x, dtype=np.float64)


def _ny_dates(stamps: list[datetime]) -> list[date]:
    out: list[date] = []
    for ts in stamps:
        if ts.tzinfo is None:
            out.append(ts.date())
        else:
            out.append(ts.astimezone(_NY).date())
    return out


def _bpy_per_name(days: list[date], printed: BoolArr) -> FloatArr:
    n_t, n_n = printed.shape
    out = np.zeros((n_t, n_n), dtype=np.float64)
    for j in range(n_n):
        left = 0
        for i in range(n_t):
            cutoff = days[i] - timedelta(days=365)
            while left < i and days[left] <= cutoff:
                left += 1
            out[i, j] = float(np.sum(printed[left : i + 1, j]))
    return out


def _wide(frame: pl.DataFrame, names: list[str], stamps: list[datetime], col: str) -> FloatArr:
    n_t, n_n = len(stamps), len(names)
    out = np.full((n_t, n_n), np.nan, dtype=np.float64)
    if n_t == 0 or n_n == 0 or col not in frame.columns:
        return out
    grid = pl.DataFrame({"ts_utc": stamps})
    piv = (
        frame.select("ts_utc", "instrument_id", col)
        .unique(subset=["ts_utc", "instrument_id"], keep="last")
        .pivot(on="instrument_id", index="ts_utc", values=col, aggregate_function="first")
        .sort("ts_utc")
    )
    aligned = grid.join(piv, on="ts_utc", how="left")
    for j, name in enumerate(names):
        if name not in aligned.columns:
            continue
        out[:, j] = aligned.get_column(name).cast(pl.Float64).to_numpy()
    return out


_HAIRCUT_ROLES = frozenset({"stock", "inverse_etf"})


def _role_haircut(role: object, haircut: float) -> float:
    return haircut if role in _HAIRCUT_ROLES else 1.0


def _write_diagnostics(frame: pl.DataFrame) -> None:
    tmp = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
    tmp.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(tmp / f"{_DIAG_PATH}.parquet")
    frame.write_csv(tmp / f"{_DIAG_PATH}.csv")


def _sync_returns(rets: FloatArr, idx: list[int], end: int, window: int) -> FloatArr | None:
    if not idx:
        return None
    start = max(0, end - window + 1)
    block = rets[start : end + 1][:, idx]
    ok = np.isfinite(block).all(axis=1)
    rows = block[ok]
    if rows.shape[0] == 0:
        return None
    return rows


def _cluster_groups(labels: IntArr, live: list[int]) -> list[list[int]]:
    groups: dict[int, list[int]] = {}
    for local, name_i in enumerate(live):
        groups.setdefault(int(labels[local]), []).append(name_i)
    return [groups[key] for key in sorted(groups)]


@register("prism")
class Prism(Strategy):
    """Clustered calendar TSMOM on a decision Panel.

    Cross-section math stays here. The Broker still fills next-open; cash is
    leftover weight, never a clustered sleeve. ``allow_cash=False`` rescales
    ON names to 100% (no vol-target de-gross) and holds the last book when
    every trend is off.
    """

    name = "prism"
    param_model = PrismParams

    def ready_bars(self, params: BaseModel) -> int:
        """Warm-up is 12 calendar months of session bars (Ledoit–Wolf window)."""
        typed = cast(PrismParams, params)
        months = max(typed.lookbacks) if typed.lookbacks else 12
        return max(typed.lw_window, months * 21)

    def targets(self, panel: pl.DataFrame, params: BaseModel) -> pl.DataFrame:
        """Emit Target weights for one decision Panel.

        See the module docstring for the session order. Null weight holds;
        0 is a full exit. Cash is leftover weight, never a clustered sleeve.
        """
        typed = cast(PrismParams, params)
        if panel.height == 0:
            return panel.select("ts_utc", "instrument_id").with_columns(pl.lit(None, dtype=pl.Float64).alias("weight"))

        work = _prepare_panel(panel, typed)
        names = sorted(work.get_column("instrument_id").unique().to_list())
        stamps = work.select("ts_utc").unique().sort("ts_utc").get_column("ts_utc").to_list()
        n_t, n_n = len(stamps), len(names)
        if n_t == 0 or n_n == 0:
            return panel.select("ts_utc", "instrument_id").with_columns(pl.lit(None, dtype=pl.Float64).alias("weight"))

        px = _wide(work, names, stamps, "px")
        rets = _wide(work, names, stamps, "ret")
        vol_fast = _wide(work, names, stamps, "vol_fast")
        vol_slow = _wide(work, names, stamps, "vol_slow")
        adv = _wide(work, names, stamps, "adv")
        min_adv = _wide(work, names, stamps, "min_adv") if "min_adv" in work.columns else np.full((n_t, n_n), np.nan)
        days = _ny_dates(stamps)
        printed = np.isfinite(px)
        bpy = _bpy_per_name(days, printed)
        vol_ann = np.asarray(np.fmax(vol_fast, vol_slow) * np.sqrt(np.maximum(bpy, 1.0)), dtype=np.float64)

        raw = np.full((n_t, n_n), np.nan, dtype=np.float64)
        trend = np.zeros((n_t, n_n), dtype=np.float64)
        for j in range(n_n):
            raw[:, j] = _calendar_raw(days, px[:, j], vol_ann[:, j], typed.lookbacks)
            trend[:, j] = apply_hysteresis(raw[:, j], typed.hysteresis)
        healthy = _healthy_mask(px, printed, typed.stale_sessions)
        history_ok = np.isfinite(raw)
        liq_cap = np.zeros((n_t, n_n), dtype=np.float64)
        positive_adv = np.maximum(np.nan_to_num(adv, nan=0.0), 0.0)
        if typed.nav_usd > 0:
            np.multiply(positive_adv, typed.adv_max_pct / typed.nav_usd, out=liq_cap)
        live = history_ok & healthy & printed & (positive_adv > 0) & (liq_cap >= typed.min_liq_weight)
        if np.isfinite(min_adv).any():
            live &= ~np.isfinite(min_adv) | (positive_adv >= min_adv)

        roles = _roles(work, names)
        ramp = _phase_ramps(live, printed, healthy, typed.phase_in, typed.phase_out)
        weights, cluster_id, cluster_cap, asset_cap, gross_scale, shrinkage, est_vol = _allocate(
            rets=rets,
            trend=trend,
            vol_ann=vol_ann,
            live=live,
            ramp=ramp,
            liq_cap=liq_cap,
            roles=roles,
            days=days,
            bpy=bpy,
            params=typed,
        )

        weight_long = _long_weights(stamps, names, weights, printed)
        out = (
            panel.select("ts_utc", "instrument_id")
            .with_row_index("_row")
            .join(weight_long, on=["ts_utc", "instrument_id"], how="left")
            .sort("_row")
            .select("ts_utc", "instrument_id", "weight")
        )
        if typed.diagnostics:
            diag = _diagnostics_frame(
                stamps=stamps,
                names=names,
                weights=weights,
                raw=raw,
                trend=trend,
                vol_ann=vol_ann,
                adv=adv,
                live=live,
                ramp=ramp,
                cluster_id=cluster_id,
                cluster_cap=cluster_cap,
                asset_cap=asset_cap,
                gross_scale=gross_scale,
                shrinkage=shrinkage,
                est_vol=est_vol,
            )
            _write_diagnostics(diag)
        return out


def _prepare_panel(panel: pl.DataFrame, params: PrismParams) -> pl.DataFrame:
    px_expr = pl.col("close")
    if "adj_close" in panel.columns:
        px_expr = pl.coalesce(pl.col("adj_close"), pl.col("close"))
    work = panel.with_columns(px_expr.alias("px"))
    prev = pl.col("px").shift(1).over("instrument_id")
    work = work.with_columns(
        pl.when((prev.is_not_null()) & (prev > 0) & pl.col("px").is_not_null())
        .then(pl.col("px") / prev - 1.0)
        .otherwise(None)
        .alias("ret")
    )
    vol_col = "volume" if "volume" in work.columns else None
    dollar = pl.lit(None, dtype=pl.Float64) if vol_col is None else pl.col("px") * pl.col(vol_col)
    work = work.with_columns(dollar.alias("dollar_vol"))
    return work.with_columns(
        ewm_std(work, params.ewma_hl_fast, col="ret").alias("vol_fast"),
        ewm_std(work, params.ewma_hl_slow, col="ret").alias("vol_slow"),
        pl.col("dollar_vol")
        .rolling_median(window_size=params.adv_window, min_samples=1)
        .over("instrument_id")
        .alias("adv"),
    )


def _healthy_mask(px: FloatArr, printed: BoolArr, stale_sessions: int) -> BoolArr:
    n_t, n_n = px.shape
    healthy = np.ones((n_t, n_n), dtype=np.bool_)
    for j in range(n_n):
        stale = 0
        last_px: float | None = None
        last_good = 0
        for i in range(n_t):
            price = px[i, j]
            if (not printed[i, j]) or (last_px is not None and price == last_px):
                stale += 1
            else:
                stale = 0
            if printed[i, j]:
                last_px = float(price)
            flagged = stale > stale_sessions
            if flagged:
                healthy[i, j] = False
            else:
                last_good = i
            if i - last_good > stale_sessions:
                healthy[i, j] = False
    return healthy


def _roles(work: pl.DataFrame, names: list[str]) -> list[str | None]:
    if "role" not in work.columns:
        return [None] * len(names)
    lookup: dict[str, str | None] = {}
    for name, role in work.select("instrument_id", "role").unique().iter_rows():
        lookup[str(name)] = None if role is None else str(role)
    return [lookup.get(name) for name in names]


def _phase_ramps(
    live: BoolArr,
    printed: BoolArr,
    healthy: BoolArr,
    phase_in: int,
    phase_out: int,
) -> FloatArr:
    n_t, n_n = live.shape
    ramp = np.zeros((n_t, n_n), dtype=np.float64)
    prev_ramp = np.zeros(n_n, dtype=np.float64)
    prev_live = np.zeros(n_n, dtype=np.bool_)
    step_in = 1.0 / phase_in
    step_out = 1.0 / phase_out
    for i in range(n_t):
        today = np.zeros(n_n, dtype=np.float64)
        for j in range(n_n):
            if live[i, j]:
                if prev_ramp[j] >= 1.0 and prev_live[j]:
                    today[j] = 1.0
                else:
                    today[j] = min(1.0, prev_ramp[j] + step_in)
            elif prev_live[j] and (not healthy[i, j]) and (not printed[i, j]):
                today[j] = 0.0
            else:
                today[j] = max(0.0, prev_ramp[j] - step_out)
        ramp[i] = today
        prev_live = live[i].copy()
        prev_ramp = today
    return ramp


def _allocate(
    *,
    rets: FloatArr,
    trend: FloatArr,
    vol_ann: FloatArr,
    live: BoolArr,
    ramp: FloatArr,
    liq_cap: FloatArr,
    roles: list[str | None],
    days: list[date],
    bpy: FloatArr,
    params: PrismParams,
) -> tuple[FloatArr, IntArr, FloatArr, FloatArr, FloatArr, FloatArr, FloatArr]:
    """Walk sessions in order. Clusters refresh when the calendar month changes.

    Returns weights, cluster id, cluster cap, per-name cap, gross scale,
    Ledoit–Wolf shrinkage, and estimated portfolio vol. The last six are
    written only when ``diagnostics`` is set.
    """
    n_t, n_n = trend.shape
    weights = np.zeros((n_t, n_n), dtype=np.float64)
    cluster_id = np.full((n_t, n_n), -1, dtype=np.int32)
    cluster_cap_out = np.zeros(n_t, dtype=np.float64)
    asset_cap = np.zeros((n_t, n_n), dtype=np.float64)
    gross_scale = np.ones(n_t, dtype=np.float64)
    shrinkage = np.full(n_t, np.nan, dtype=np.float64)
    est_vol = np.full(n_t, np.nan, dtype=np.float64)
    month_key = ""
    clusters: list[list[int]] = []

    for i in range(n_t):
        live_idx = [j for j in range(n_n) if live[i, j]]
        mk = f"{days[i].year:04d}-{days[i].month:02d}"
        if mk != month_key:
            month_key = mk
            if live_idx:
                start = max(0, i - params.lw_window + 1)
                rho = _corr_matrix(rets[start : i + 1], live_idx)
                labels = cluster_labels(rho, params.corr_cut)
                clusters = _cluster_groups(labels, live_idx)
            else:
                clusters = []
        cov_idx = [j for j in range(n_n) if live[i, j] or ramp[i, j] > 0]
        cov: FloatArr | None = None
        rows = _sync_returns(rets, cov_idx, i, params.lw_window)
        if rows is not None and rows.shape[0] >= params.lw_seed and rows.shape[1] >= 1:
            if rows.shape[0] >= 2:
                sub, sh = ledoit_wolf(rows)
                shrinkage[i] = sh
                cov = np.zeros((n_n, n_n), dtype=np.float64)
                for a, ja in enumerate(cov_idx):
                    for b, jb in enumerate(cov_idx):
                        cov[ja, jb] = sub[a, b]
            elif rows.shape[1] == 1:
                cov = np.zeros((n_n, n_n), dtype=np.float64)
                cov[cov_idx[0], cov_idx[0]] = float(np.var(rows[:, 0]))
                shrinkage[i] = 0.0

        w, assigned, c_cap, caps, gscale, gvol = _targets_on_day(
            i=i,
            live_idx=live_idx,
            clusters=clusters,
            trend=trend[i],
            vol_ann=vol_ann[i],
            ramp=ramp[i],
            liq_cap=liq_cap[i],
            roles=roles,
            cov=cov,
            bpy=float(np.nanmean(bpy[i, cov_idx])) if cov_idx else float(np.nanmean(bpy[i])),
            params=params,
        )
        if not params.allow_cash:
            w, gscale, gvol = _fully_invest(
                w,
                gscale,
                gvol,
                prev=weights[i - 1] if i else None,
                cov=cov,
                bpy=float(np.nanmean(bpy[i, cov_idx])) if cov_idx else float(np.nanmean(bpy[i])),
                allow_short=params.allow_short,
            )
        weights[i] = w
        cluster_id[i] = assigned
        cluster_cap_out[i] = c_cap
        asset_cap[i] = caps
        gross_scale[i] = gscale
        est_vol[i] = gvol
    return weights, cluster_id, cluster_cap_out, asset_cap, gross_scale, shrinkage, est_vol


def _targets_on_day(
    *,
    i: int,
    live_idx: list[int],
    clusters: list[list[int]],
    trend: FloatArr,
    vol_ann: FloatArr,
    ramp: FloatArr,
    liq_cap: FloatArr,
    roles: list[str | None],
    cov: FloatArr | None,
    bpy: float,
    params: PrismParams,
) -> tuple[FloatArr, IntArr, float, FloatArr, float, float]:
    del i
    n_n = int(trend.size)
    w: FloatArr = np.zeros(n_n, dtype=np.float64)
    assigned: IntArr = np.full(n_n, -1, dtype=np.int32)
    caps: FloatArr = np.zeros(n_n, dtype=np.float64)
    scored = [j for j in live_idx if ramp[j] > 0]
    if not scored:
        return w, assigned, 0.0, caps, 1.0, float("nan")

    clist: list[list[int]] = []
    used: set[int] = set()
    for group in clusters:
        members = [j for j in group if j in scored]
        if not members:
            continue
        cid = len(clist)
        clist.append(members)
        for j in members:
            assigned[j] = cid
            used.add(j)
    for j in scored:
        if j not in used:
            assigned[j] = len(clist)
            clist.append([j])
    n_c = len(clist)
    c_cap = max(params.cluster_cap_floor, params.cluster_cap_num / n_c) if n_c else params.cluster_cap_floor

    budgets: list[float] = []
    for members in clist:
        sig = float(np.mean(trend[members]))
        inv = np.array(
            [0.0 if (not np.isfinite(vol_ann[j]) or vol_ann[j] <= 0) else 1.0 / vol_ann[j] for j in members],
            dtype=np.float64,
        )
        s_inv = float(inv.sum())
        basket: FloatArr = np.zeros(n_n, dtype=np.float64)
        if s_inv > 0:
            for j, share in zip(members, inv, strict=True):
                basket[j] = share / s_inv
        if cov is not None and s_inv > 0:
            cvol = _port_vol(basket, cov, bpy)
        else:
            cvol = 0.0
            if s_inv > 0:
                for j, share in zip(members, inv, strict=True):
                    vol = vol_ann[j] if np.isfinite(vol_ann[j]) else 0.0
                    cvol += (share / s_inv) ** 2 * vol * vol
                cvol = math.sqrt(max(cvol, 0.0))
        if cvol <= 1e-12 or sig <= 0:
            budgets.append(0.0)
        else:
            budgets.append(sig / cvol)
    s_b = sum(budgets)
    if s_b <= 0:
        return w, assigned, c_cap, caps, 1.0, float("nan")
    budgets = [b / s_b for b in budgets]

    for cid, members in enumerate(clist):
        scores: list[float] = []
        for j in members:
            vol = vol_ann[j]
            tr = trend[j]
            if not np.isfinite(vol) or vol <= 0 or tr <= 0:
                scores.append(0.0)
            else:
                scores.append(tr / vol)
        s_sc = sum(scores)
        if s_sc <= 0:
            continue
        for j, score in zip(members, scores, strict=True):
            w[j] = budgets[cid] * (score / s_sc)

    for j in range(n_n):
        if assigned[j] < 0 or trend[j] <= 0:
            caps[j] = 0.0
            continue
        vol = vol_ann[j]
        scale = 1.0 if (not np.isfinite(vol) or vol <= 0) else min(1.0, params.ref_vol / vol)
        cap = c_cap * scale * _role_haircut(roles[j], params.stock_haircut)
        if liq_cap[j] > 0:
            cap = min(cap, float(liq_cap[j]))
        caps[j] = cap

    cluster_ids = [int(cid) for cid in assigned]
    w = _waterfill(w, caps, cluster_ids, clist, c_cap)
    w = np.asarray(np.where(trend <= 0, 0.0, w), dtype=np.float64)
    w *= np.maximum(ramp, 0.0)
    if not params.allow_short:
        np.maximum(w, 0.0, out=w)

    gvol = float("nan")
    gscale = 1.0
    if cov is not None:
        gvol = _port_vol(w, cov, bpy)
        if params.allow_cash:
            gscale = 1.0 if gvol <= 0 else min(1.0, params.vol_target / gvol)
            w *= gscale
    gross = float(np.sum(np.abs(w))) if params.allow_short else float(np.sum(w))
    if params.allow_cash and gross > 1.0:
        w /= gross
        gscale *= 1.0 / gross
    return w, assigned, c_cap, caps, gscale, gvol


def _fully_invest(
    weights: FloatArr,
    gscale: float,
    gvol: float,
    *,
    prev: FloatArr | None,
    cov: FloatArr | None,
    bpy: float,
    allow_short: bool,
) -> tuple[FloatArr, float, float]:
    """Rescale ON weights to 100%. If none are ON, keep yesterday's book."""
    w = np.asarray(weights, dtype=np.float64).copy()
    gross = float(np.sum(np.abs(w))) if allow_short else float(np.sum(w))
    if gross > _EPS:
        w /= gross
        scaled = gscale / gross
        port_vol = _port_vol(w, cov, bpy) if cov is not None else gvol
        return w, scaled, port_vol
    if prev is not None:
        held = np.asarray(prev, dtype=np.float64).copy()
        port_vol = _port_vol(held, cov, bpy) if cov is not None else gvol
        return held, 1.0, port_vol
    return w, gscale, gvol


def _long_weights(
    stamps: list[datetime],
    names: list[str],
    weights: FloatArr,
    printed: BoolArr,
) -> pl.DataFrame:
    ts_col: list[datetime] = []
    id_col: list[str] = []
    w_col: list[float | None] = []
    n_t, n_n = weights.shape
    for i in range(n_t):
        for j in range(n_n):
            ts_col.append(stamps[i])
            id_col.append(names[j])
            if not printed[i, j]:
                w_col.append(None)
            else:
                w_col.append(float(weights[i, j]))
    return pl.DataFrame({"ts_utc": ts_col, "instrument_id": id_col, "weight": w_col})


def _diagnostics_frame(
    *,
    stamps: list[datetime],
    names: list[str],
    weights: FloatArr,
    raw: FloatArr,
    trend: FloatArr,
    vol_ann: FloatArr,
    adv: FloatArr,
    live: BoolArr,
    ramp: FloatArr,
    cluster_id: IntArr,
    cluster_cap: FloatArr,
    asset_cap: FloatArr,
    gross_scale: FloatArr,
    shrinkage: FloatArr,
    est_vol: FloatArr,
) -> pl.DataFrame:
    n_t, n_n = weights.shape
    rows: list[dict[str, object]] = []
    for i in range(n_t):
        for j in range(n_n):
            cid = int(cluster_id[i, j])
            rows.append(
                {
                    "ts_utc": stamps[i],
                    "instrument_id": names[j],
                    "weight": float(weights[i, j]),
                    "raw": None if not np.isfinite(raw[i, j]) else float(raw[i, j]),
                    "trend": float(trend[i, j]),
                    "vol_ann": None if not np.isfinite(vol_ann[i, j]) else float(vol_ann[i, j]),
                    "adv": None if not np.isfinite(adv[i, j]) else float(adv[i, j]),
                    "eligible": bool(live[i, j]),
                    "ramp": float(ramp[i, j]),
                    "cluster_id": None if cid < 0 else cid,
                    "cluster_cap": float(cluster_cap[i]),
                    "asset_cap": float(asset_cap[i, j]),
                    "gross_scale": float(gross_scale[i]),
                    "shrinkage": None if not np.isfinite(shrinkage[i]) else float(shrinkage[i]),
                    "est_vol": None if not np.isfinite(est_vol[i]) else float(est_vol[i]),
                }
            )
    return pl.DataFrame(
        rows,
        schema={
            "ts_utc": pl.Datetime("us", "UTC"),
            "instrument_id": pl.String,
            "weight": pl.Float64,
            "raw": pl.Float64,
            "trend": pl.Float64,
            "vol_ann": pl.Float64,
            "adv": pl.Float64,
            "eligible": pl.Boolean,
            "ramp": pl.Float64,
            "cluster_id": pl.Int32,
            "cluster_cap": pl.Float64,
            "asset_cap": pl.Float64,
            "gross_scale": pl.Float64,
            "shrinkage": pl.Float64,
            "est_vol": pl.Float64,
        },
        infer_schema_length=0,
    )


__all__ = [
    "Prism",
    "PrismParams",
    "apply_hysteresis",
    "cluster_labels",
    "ledoit_wolf",
]
