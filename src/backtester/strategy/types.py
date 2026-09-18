"""Strategy-engine types: Fill, Metrics, Comparison, BrokerConfig, RunResult."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import polars as pl
from pydantic import BaseModel, ConfigDict, Field


class BrokerConfig(BaseModel):
    """Execution knobs shared by every Run on a Spec.

    Attributes:
        rebalance_band: Trade only when ``|target − held|`` meets this threshold.
        allow_short: Broker gate; negative Targets become flat when False.
        initial_equity: Starting NAV (cash). Returns scale independently of this.
    """

    model_config = ConfigDict(frozen=True)

    rebalance_band: float = 0.0
    allow_short: bool = True
    initial_equity: float = 1.0


class Fill(BaseModel):
    """A Broker trade at next-bar open. Strategies never emit Fills."""

    model_config = ConfigDict(frozen=True)

    ts_utc: datetime
    instrument_id: str
    size: float
    price: float
    fee: float


class Metrics(BaseModel):
    """Windowed performance for one Run, aligned at ``ready_bars``."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    strategy: str
    total_return: float
    max_drawdown: float
    trades: int
    win_rate: float
    turnover: float
    ending_equity: float
    start_ts: datetime
    end_ts: datetime
    ready_bars: int
    baseline_return: float | None = None
    excess_return: float | None = None


class Comparison(BaseModel):
    """Side-by-side Metrics for Runs that shared one Panel."""

    model_config = ConfigDict(frozen=True)

    spec_name: str
    runs: tuple[Metrics, ...] = Field(default_factory=tuple)
    chart_path: str | None = None


@dataclass
class RunResult:
    """One executed Run: Targets, Fills, equity curve, Metrics."""

    run_id: str
    strategy: str
    params: dict[str, object]
    targets: pl.DataFrame
    fills: pl.DataFrame
    equity: pl.DataFrame
    metrics: Metrics
    ready_bars: int
