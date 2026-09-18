"""Strategy engine: Panel → Targets → Broker → Metrics → Compare."""

from __future__ import annotations

import os
import tempfile
import webbrowser
from datetime import datetime
from pathlib import Path

import polars as pl
from pydantic import ValidationError

from backtester.exceptions import InvalidSpecError
from backtester.store.interface import BarStore
from backtester.strategy.broker import execute
from backtester.strategy.panel import load_panel
from backtester.strategy.registry import get, load_plugin
from backtester.strategy.spec import Spec, load_spec
from backtester.strategy.types import BrokerConfig, Comparison, Metrics, RunResult

_METRICS_HEADER = (
    f"{'run':<12} {'strategy':<12} {'ret':>10} {'maxdd':>10} {'trades':>7} "
    f"{'win':>8} {'turn':>8} {'end':>10} {'excess':>10}"
)


def run_spec(spec: Spec, store: BarStore) -> list[RunResult]:
    """Load one Panel, execute every Run, return Metrics-aligned results.

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
        allow_short=spec.allow_short,
        initial_equity=spec.initial_equity,
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
        fills, equity = execute(panel, targets, config)
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
    return _with_baseline(results)


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
    peak = window["equity"].cum_max()
    dd_vals = [float(v) for v in (window["equity"] / peak - 1.0).to_list() if isinstance(v, int | float)]
    max_dd = min(dd_vals) if dd_vals else 0.0
    fills_w = fills.filter(pl.col("ts_utc") >= start_ts)
    trades = int(fills_w.height)
    rets = window["r"].to_list()
    win_rate = sum(1 for r in rets if r is not None and r > 0.0) / len(rets) if rets else 0.0
    turnover = float(window["turnover"].sum())
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
    )


def format_metrics_table(metrics: list[Metrics] | tuple[Metrics, ...]) -> str:
    """Render a stdout Metrics table."""
    lines = [_METRICS_HEADER]
    for row in metrics:
        excess = f"{row.excess_return:10.4f}" if row.excess_return is not None else f"{'n/a':>10}"
        lines.append(
            f"{row.run_id:<12} {row.strategy:<12} {row.total_return:10.4f} {row.max_drawdown:10.4f} "
            f"{row.trades:7d} {row.win_rate:8.4f} {row.turnover:8.4f} {row.ending_equity:10.4f} {excess}"
        )
    return "\n".join(lines)


def write_equity_chart(
    spec_name: str,
    results: list[RunResult],
    *,
    open_browser: bool = True,
    out_dir: Path | None = None,
) -> Path:
    """Plotly equity overlay, rebased at the latest ready timestamp.

    Args:
        spec_name: Chart title prefix.
        results: Executed Runs (equity frames).
        open_browser: Open the HTML unless tests pass ``False``.
        out_dir: Directory for the file (default ``$TMPDIR``).

    Returns:
        Path to the self-contained HTML.
    """
    import plotly.graph_objects as go

    if not results:
        raise InvalidSpecError("no Runs to chart")
    align = max(r.metrics.start_ts for r in results)
    fig = go.Figure()
    run_ids = [r.run_id for r in results]
    for result in results:
        frame = result.equity.filter(pl.col("ts_utc") >= align).sort("ts_utc")
        if frame.height == 0:
            continue
        base = float(frame["equity"][0])
        y = [float(v) / base for v in frame["equity"].to_list()] if base else frame["equity"].to_list()
        fig.add_trace(
            go.Scatter(
                x=frame["ts_utc"].to_list(),
                y=y,
                mode="lines",
                name=result.run_id,
            )
        )
    fig.update_layout(
        title=f"{spec_name} equity overlay ({', '.join(run_ids)})",
        xaxis_title="ts_utc",
        yaxis_title="rebased equity",
        template="plotly_white",
    )
    folder = out_dir or Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"strat-compare-{spec_name}.html"
    fig.write_html(str(path), include_plotlyjs=True, full_html=True)
    if open_browser:
        webbrowser.open(path.as_uri())
    return path


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
