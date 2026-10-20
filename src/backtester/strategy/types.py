"""Strategy-engine types: Fill, Metrics, Comparison, BrokerConfig, RunResult."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import polars as pl
from pydantic import BaseModel, ConfigDict, Field


class BrokerConfig(BaseModel):
    """Execution knobs shared by every Run on a Spec.

    Attributes:
        rebalance_band: Flat hysteresis. Trade when ``|target − held|`` meets
            this threshold. Used when ``band_floor`` and ``band_frac`` are 0.
        band_floor: Proportional-band floor. With ``band_frac``, threshold is
            ``max(band_floor, band_frac * |target|)``.
        band_frac: Proportional-band fraction of ``|target|``.
        half_move: When a trade triggers, move halfway to target. A target of
            0 always fully exits.
        allow_short: Broker gate; negative Targets become flat when False.
        initial_equity: Starting NAV (cash). Returns scale independently of this.
        drift: When True, weights follow price between Targets. A null Target
            keeps the drifted weight; the next non-null Target resets it.
            False (default) holds the last Target weight (constant mix).
    """

    model_config = ConfigDict(frozen=True)

    rebalance_band: float = 0.0
    band_floor: float = 0.0
    band_frac: float = 0.0
    half_move: bool = False
    allow_short: bool = True
    initial_equity: float = 1.0
    drift: bool = False


class Fill(BaseModel):
    """A Broker trade at next-bar open. Strategies never emit Fills."""

    model_config = ConfigDict(frozen=True)

    ts_utc: datetime
    instrument_id: str
    size: float
    price: float
    fee: float


class Metrics(BaseModel):
    """Windowed performance for one Run, aligned at ``ready_bars``.

    ``growth_100`` is the ending value of $100 invested at the ready-bar start:
    ``100 * (1 + total_return)``. SIP fields overlay $100 on the first decision
    day of each calendar month in this window (``sip.py``); they do not change
    Fills or Strategy Targets. ``sip_cagr`` is money-weighted XIRR of those
    contributions, not lump-sum time-weighted CAGR. ``sip_max_drawdown`` is
    peak-to-trough on the SIP wealth series after contributions.
    """

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
    cagr: float = 0.0
    growth_100: float = 100.0
    sip_contributed: float = 0.0
    sip_value: float = 0.0
    sip_multiple: float = 0.0
    sip_cagr: float = 0.0
    sip_max_drawdown: float = 0.0
    ann_vol: float = 0.0
    sharpe_excess: float = 0.0
    sharpe_raw: float = 0.0
    sortino: float = 0.0
    calmar: float = 0.0
    mean_gross: float = 0.0
    mean_cash: float = 0.0
    annual_turnover: float = 0.0
    fill_count: int = 0
    best_year: int | None = None
    worst_year: int | None = None
    best_year_return: float | None = None
    worst_year_return: float | None = None


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
