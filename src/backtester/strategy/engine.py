"""Strategy engine: Panel → Targets → Broker → Metrics → Compare."""

from __future__ import annotations

from datetime import datetime
from math import sqrt
from pathlib import Path
from typing import TypedDict

import polars as pl
from pydantic import ValidationError

from backtester.exceptions import InvalidSpecError
from backtester.store.interface import BarStore
from backtester.strategy.broker import execute
from backtester.strategy.panel import load_panel
from backtester.strategy.registry import get, load_plugin
from backtester.strategy.sip import MONTHLY_SIP_DOLLARS, sip_wealth
from backtester.strategy.spec import Spec, load_spec
from backtester.strategy.types import BrokerConfig, Comparison, Metrics, RunResult

_SIP_VALUE_LABEL = f"SIP ${MONTHLY_SIP_DOLLARS:.0f}/mo"
_METRICS_HEADER = (
    f"{'run':<12} {'strategy':<12} {'ret':>10} {'CAGR':>10} {'$100':>10} "
    f"{_SIP_VALUE_LABEL:>12} {'SIP in':>10} {'SIP x':>8} "
    f"{'SIP CAGR':>10} {'SIP maxdd':>10} "
    f"{'maxdd':>10} {'trades':>7} {'win':>8} {'turn':>8} {'end':>10} {'excess':>10}"
)


class _AnnMetrics(TypedDict):
    cagr: float
    ann_vol: float
    sharpe_excess: float
    sharpe_raw: float
    sortino: float
    calmar: float
    mean_gross: float
    mean_cash: float
    annual_turnover: float
    best_year: int | None
    worst_year: int | None
    best_year_return: float | None
    worst_year_return: float | None


def run_spec(spec: Spec, store: BarStore) -> list[RunResult]:
    """Load one Panel, execute every Run, return Metrics-aligned results.

    Metrics for every Run share the latest ``ready_bars`` on the Spec so a
    buy-and-hold comparison starts when the slowest Strategy is ready, not at
    listing.

    Args:
        spec: Shared Universe and Runs.
        store: Coverage-backed BarStore (no fetch).

    Returns:
        One ``RunResult`` per Spec run, in Spec order.

    Raises:
        InvalidSpecError: Unknown Strategy or params that fail ``param_model``.
        MissingCoverageError: Panel cannot be loaded from Coverage.
    """
    for path in spec.plugins:
        load_plugin(path)
    _validate_runs(spec)
    panel = load_panel(spec, store)
    config = BrokerConfig(
        rebalance_band=spec.rebalance_band,
        band_floor=spec.band_floor,
        band_frac=spec.band_frac,
        half_move=spec.half_move,
        allow_short=spec.allow_short,
        initial_equity=spec.initial_equity,
        drift=spec.drift,
    )
    stamps = panel.select("ts_utc").unique().sort("ts_utc").get_column("ts_utc").to_list()
    results: list[RunResult] = []
    for run in spec.runs:
        cls = get(run.strategy)
        strategy = cls()
        try:
            params = strategy.param_model.model_validate(run.params)
        except ValidationError as exc:
            raise InvalidSpecError(f"run {run.id}: {exc}") from exc
        ready = strategy.ready_bars(params)
        targets = strategy.targets(panel, params)
        run_config = config if run.drift is None else config.model_copy(update={"drift": run.drift})
        fills, equity = execute(panel, targets, run_config)
        metrics = compute_metrics(
            run_id=run.id,
            strategy_name=run.strategy,
            equity=equity,
            fills=fills,
            ready_bars=ready,
            panel_ts=stamps,
        )
        results.append(
            RunResult(
                run_id=run.id,
                strategy=run.strategy,
                params=dict(run.params),
                targets=targets,
                fills=fills,
                equity=equity,
                metrics=metrics,
                ready_bars=ready,
            )
        )
    return _with_baseline(_align_metrics_window(results, stamps))


def compare_spec(spec: Spec, store: BarStore) -> Comparison:
    """Run the Spec and pack Metrics into a Comparison."""
    results = run_spec(spec, store)
    return Comparison(
        spec_name=spec.name,
        runs=tuple(r.metrics for r in results),
    )


def compute_metrics(
    *,
    run_id: str,
    strategy_name: str,
    equity: pl.DataFrame,
    fills: pl.DataFrame,
    ready_bars: int,
    panel_ts: list[datetime],
    baseline_equity: pl.DataFrame | None = None,
) -> Metrics:
    """Slice the equity curve at the first ready bar and score the window.

    Win rate is the fraction of Metrics-window bars with a positive return.
    Turnover is the sum of absolute weight changes in the window.
    ``sharpe_excess`` is mean(r − rf) / vol (rf from ``daily_tbill``);
    ``sharpe_raw`` is mean(r) / vol (unlabeled ret/vol).
    SIP is $100 on the first decision day of each calendar month in this window.
    SIP CAGR is XIRR of those cashflows; SIP maxdd is peak-to-trough on SIP wealth.
    """
    if not panel_ts:
        raise InvalidSpecError("Panel has no timestamps")
    if ready_bars >= len(panel_ts):
        raise InvalidSpecError(f"run {run_id}: ready_bars={ready_bars} exceeds Panel length {len(panel_ts)}")
    start_ts = panel_ts[ready_bars]
    window = equity.filter(pl.col("ts_utc") >= start_ts).sort("ts_utc")
    if window.height == 0:
        raise InvalidSpecError(f"run {run_id}: no equity rows after ready_bars={ready_bars}")
    start_equity = float(window["equity"][0])
    end_equity = float(window["equity"][-1])
    total_return = (end_equity / start_equity - 1.0) if start_equity else 0.0
    growth_100 = 100.0 * (1.0 + total_return)
    sip_rets = [float(v) if v is not None else 0.0 for v in window["r"].to_list()]
    sip = sip_wealth(window["ts_utc"].to_list(), sip_rets, contribution=MONTHLY_SIP_DOLLARS)
    peak = window["equity"].cum_max()
    dd_vals = [float(v) for v in (window["equity"] / peak - 1.0).to_list() if isinstance(v, int | float)]
    max_dd = min(dd_vals) if dd_vals else 0.0
    fills_w = fills.filter(pl.col("ts_utc") >= start_ts)
    trades = int(fills_w.height)
    rets = [float(v) for v in window["r"].to_list() if v is not None]
    win_rate = sum(1 for r in rets if r > 0.0) / len(rets) if rets else 0.0
    turnover = float(window["turnover"].sum())
    extra = _annualized_metrics(window, start_equity, end_equity, max_dd, turnover)
    baseline_return = None
    excess = None
    if baseline_equity is not None:
        baseline_return = _return_on_window(baseline_equity, start_ts)
        excess = total_return - baseline_return
    return Metrics(
        run_id=run_id,
        strategy=strategy_name,
        total_return=total_return,
        max_drawdown=max_dd,
        trades=trades,
        win_rate=win_rate,
        turnover=turnover,
        ending_equity=end_equity,
        start_ts=start_ts,
        end_ts=window["ts_utc"][-1],
        ready_bars=ready_bars,
        baseline_return=baseline_return,
        excess_return=excess,
        fill_count=trades,
        growth_100=growth_100,
        sip_contributed=sip.total_contributed,
        sip_value=sip.ending_value,
        sip_multiple=sip.multiple,
        sip_cagr=sip.cagr,
        sip_max_drawdown=sip.max_drawdown,
        **extra,
    )


def format_metrics_table(metrics: list[Metrics] | tuple[Metrics, ...]) -> str:
    """Render a stdout Metrics table with lump-sum CAGR/$100/maxdd and SIP XIRR/maxdd."""
    lines = [_METRICS_HEADER]
    for row in metrics:
        excess = f"{row.excess_return:10.4f}" if row.excess_return is not None else f"{'n/a':>10}"
        lines.append(
            f"{row.run_id:<12} {row.strategy:<12} {row.total_return:10.4f} {row.cagr:10.4f} "
            f"{row.growth_100:10.2f} {row.sip_value:12.2f} {row.sip_contributed:10.2f} "
            f"{row.sip_multiple:8.2f} {row.sip_cagr:10.4f} {row.sip_max_drawdown:10.4f} "
            f"{row.max_drawdown:10.4f} {row.trades:7d} {row.win_rate:8.4f} "
            f"{row.turnover:8.4f} {row.ending_equity:10.4f} {excess}"
        )
    return "\n".join(lines)


def write_equity_chart(
    spec_name: str,
    results: list[RunResult],
    *,
    open_browser: bool = True,
    out_dir: Path | None = None,
) -> Path:
    """Write the monthly interactive Compare webpage (growth, allocation, scorecard)."""
    from backtester.strategy.dashboard import write_compare_page

    return write_compare_page(spec_name, results, open_browser=open_browser, out_dir=out_dir)


def run_yaml(path: Path | str, store: BarStore) -> list[RunResult]:
    """Load a Spec file and execute it."""
    return run_spec(load_spec(path), store)


def _validate_runs(spec: Spec) -> None:
    for run in spec.runs:
        cls = get(run.strategy)
        try:
            cls.param_model.model_validate(run.params)
        except ValidationError as exc:
            raise InvalidSpecError(f"run {run.id}: {exc}") from exc


def _align_metrics_window(results: list[RunResult], panel_ts: list[datetime]) -> list[RunResult]:
    """Re-score every Run from the latest ready timestamp on the Spec."""
    align_ready = max(result.ready_bars for result in results)
    aligned: list[RunResult] = []
    for result in results:
        metrics = compute_metrics(
            run_id=result.run_id,
            strategy_name=result.strategy,
            equity=result.equity,
            fills=result.fills,
            ready_bars=align_ready,
            panel_ts=panel_ts,
        )
        aligned.append(
            RunResult(
                run_id=result.run_id,
                strategy=result.strategy,
                params=result.params,
                targets=result.targets,
                fills=result.fills,
                equity=result.equity,
                metrics=metrics,
                ready_bars=result.ready_bars,
            )
        )
    return aligned


def _with_baseline(results: list[RunResult]) -> list[RunResult]:
    baseline = next((r for r in results if r.strategy == "buy_hold"), None)
    if baseline is None:
        return results
    aligned: list[RunResult] = []
    for result in results:
        baseline_return = _return_on_window(baseline.equity, result.metrics.start_ts)
        metrics = result.metrics.model_copy(
            update={
                "baseline_return": baseline_return,
                "excess_return": result.metrics.total_return - baseline_return,
            }
        )
        aligned.append(
            RunResult(
                run_id=result.run_id,
                strategy=result.strategy,
                params=result.params,
                targets=result.targets,
                fills=result.fills,
                equity=result.equity,
                metrics=metrics,
                ready_bars=result.ready_bars,
            )
        )
    return aligned


def _return_on_window(equity: pl.DataFrame, start_ts: datetime) -> float:
    window = equity.filter(pl.col("ts_utc") >= start_ts).sort("ts_utc")
    if window.height == 0:
        return 0.0
    start = float(window["equity"][0])
    end = float(window["equity"][-1])
    if start == 0.0:
        return 0.0
    return end / start - 1.0


def _annualized_metrics(
    window: pl.DataFrame,
    start_equity: float,
    end_equity: float,
    max_dd: float,
    turnover: float,
) -> _AnnMetrics:
    start_ts = window["ts_utc"][0]
    end_ts = window["ts_utc"][-1]
    span_seconds = (end_ts - start_ts).total_seconds()
    years = span_seconds / (365.25 * 24 * 3600) if span_seconds > 0 else 1.0 / 365.25
    cagr = (end_equity / start_equity) ** (1.0 / years) - 1.0 if start_equity > 0 else 0.0
    rets = [float(v) if v is not None else 0.0 for v in window["r"].to_list()]
    n = len(rets)
    ppy = n / years if years > 0 else 0.0
    mean_r = sum(rets) / n if n else 0.0
    var = sum((r - mean_r) ** 2 for r in rets) / (n - 1) if n > 1 else 0.0
    std = sqrt(var)
    ann_vol = std * sqrt(ppy) if ppy > 0 else 0.0
    if "daily_tbill" in window.columns:
        rf_vals = [float(v) if v is not None else 0.0 for v in window["daily_tbill"].to_list()]
    else:
        rf_vals = [0.0] * n
    if len(rf_vals) != n:
        rf_vals = (rf_vals + [0.0] * n)[:n]
    excess = [r - rf for r, rf in zip(rets, rf_vals, strict=True)]
    mean_ex = sum(excess) / n if n else 0.0
    scale = sqrt(ppy) if ppy > 0 and std > 0 else 0.0
    sharpe_raw = (mean_r / std) * scale if std > 0 else 0.0
    sharpe_excess = (mean_ex / std) * scale if std > 0 else 0.0
    down = [min(x, 0.0) for x in excess]
    down_var = sum(d * d for d in down) / n if n else 0.0
    down_std = sqrt(down_var)
    sortino = (mean_ex / down_std) * sqrt(ppy) if down_std > 0 and ppy > 0 else 0.0
    calmar = cagr / abs(max_dd) if max_dd < 0 else 0.0
    mean_gross = _mean_col(window, "gross")
    if "cash_weight" in window.columns:
        mean_cash = _mean_col(window, "cash_weight")
    elif "gross" in window.columns:
        cash_w = (1.0 - window["gross"]).clip(lower_bound=0.0)
        mean_cash = _mean_col(window.with_columns(cash_w.alias("_cash_w")), "_cash_w")
    else:
        mean_cash = 0.0
    annual_turnover = turnover / years if years > 0 else 0.0
    best_year, worst_year, best_ret, worst_ret = _calendar_years(window)
    return {
        "cagr": cagr,
        "ann_vol": ann_vol,
        "sharpe_excess": sharpe_excess,
        "sharpe_raw": sharpe_raw,
        "sortino": sortino,
        "calmar": calmar,
        "mean_gross": mean_gross,
        "mean_cash": mean_cash,
        "annual_turnover": annual_turnover,
        "best_year": best_year,
        "worst_year": worst_year,
        "best_year_return": best_ret,
        "worst_year_return": worst_ret,
    }


def _mean_col(frame: pl.DataFrame, name: str) -> float:
    if name not in frame.columns:
        return 0.0
    val = frame[name].mean()
    if isinstance(val, int | float) and not isinstance(val, bool):
        return float(val)
    return 0.0


def _calendar_years(window: pl.DataFrame) -> tuple[int | None, int | None, float | None, float | None]:
    if window.height == 0 or "r" not in window.columns:
        return None, None, None, None
    yearly = (
        window.with_columns(pl.col("ts_utc").dt.year().alias("year"))
        .group_by("year")
        .agg(((1.0 + pl.col("r")).product() - 1.0).alias("yr_ret"))
        .sort("year")
    )
    if yearly.height == 0:
        return None, None, None, None
    rows = yearly.to_dicts()
    best = max(rows, key=lambda row: float(row["yr_ret"]))
    worst = min(rows, key=lambda row: float(row["yr_ret"]))
    return int(best["year"]), int(worst["year"]), float(best["yr_ret"]), float(worst["yr_ret"])
