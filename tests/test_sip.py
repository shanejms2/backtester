"""Monthly SIP overlay on a Metrics return series. No live APIs."""

from __future__ import annotations

from datetime import UTC, datetime

import polars as pl
import pytest

from backtester.strategy.engine import compute_metrics, format_metrics_table
from backtester.strategy.sip import MONTHLY_SIP_DOLLARS, sip_wealth

# Known 2-month path (hand-computed):
# 2024-01-15 r=0.10 SIP $100 → 0 * 1.10 + 100 = 100
# 2024-01-16 r=0.00          → 100 * 1.00 + 0 = 100
# 2024-02-01 r=0.20 SIP $100 → 100 * 1.20 + 100 = 220
# 2024-02-02 r=-0.05         → 220 * 0.95 + 0 = 209
# Wealth peak 220 then 209 → SIP maxdd = 209/220 - 1 = -0.05
# Cashflows: 2024-01-15 -100, 2024-02-01 -100, 2024-02-02 +209
_TWO_MONTH_STAMPS = [
    datetime(2024, 1, 15, 21, 0, tzinfo=UTC),
    datetime(2024, 1, 16, 21, 0, tzinfo=UTC),
    datetime(2024, 2, 1, 21, 0, tzinfo=UTC),
    datetime(2024, 2, 2, 21, 0, tzinfo=UTC),
]
_TWO_MONTH_RETS = [0.10, 0.00, 0.20, -0.05]
_TWO_MONTH_END = 209.0
_TWO_MONTH_CONTRIBUTED = 200.0
_TWO_MONTH_MAXDD = 209.0 / 220.0 - 1.0
_YEAR_SECONDS = 365.0 * 24.0 * 3600.0

# 365-day XIRR fixture (2023-01-15 → 2024-01-15 is 365 days):
# 2023-01-15 r=0.00 SIP $100 → 100
# 2024-01-15 r=0.10 SIP $100 → 100 * 1.10 + 100 = 210
# Cashflows: -100, then -100 + 210 = +110 one year later → XIRR = 10%
_XIRR_STAMPS = [
    datetime(2023, 1, 15, 21, 0, tzinfo=UTC),
    datetime(2024, 1, 15, 21, 0, tzinfo=UTC),
]
_XIRR_RETS = [0.00, 0.10]

# SIP maxdd ≠ lump-sum maxdd:
# 2024-01-15 r=0.00 SIP $100 → 100
# 2024-01-16 r=-0.50         → 50    (SIP dd = -50%)
# 2024-02-01 r=0.00 SIP $100 → 150
# 2024-02-02 r=-0.20         → 120   (SIP dd = -20%)
# SIP maxdd = -0.50; lump-sum NAV 1.0 → 0.4 → maxdd = -0.60
_DIVERGE_STAMPS = [
    datetime(2024, 1, 15, 21, 0, tzinfo=UTC),
    datetime(2024, 1, 16, 21, 0, tzinfo=UTC),
    datetime(2024, 2, 1, 21, 0, tzinfo=UTC),
    datetime(2024, 2, 2, 21, 0, tzinfo=UTC),
]
_DIVERGE_RETS = [0.00, -0.50, 0.00, -0.20]
_DIVERGE_SIP_MAXDD = -0.50
_DIVERGE_LUMP_MAXDD = -0.60


def _empty_fills() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts_utc": pl.Series([], dtype=pl.Datetime(time_zone="UTC")),
            "instrument_id": pl.Series([], dtype=pl.Utf8),
            "size": pl.Series([], dtype=pl.Float64),
            "price": pl.Series([], dtype=pl.Float64),
            "fee": pl.Series([], dtype=pl.Float64),
        }
    )


def _equity_from_returns(stamps: list[datetime], rets: list[float]) -> pl.DataFrame:
    equity_vals = []
    nav = 1.0
    for ret in rets:
        nav *= 1.0 + ret
        equity_vals.append(nav)
    return pl.DataFrame(
        {
            "ts_utc": stamps,
            "equity": equity_vals,
            "r": rets,
            "turnover": [0.0] * len(stamps),
        }
    ).with_columns(pl.col("ts_utc").dt.convert_time_zone("UTC"))


def _npv(rate: float, stamps: list[datetime], amounts: list[float]) -> float:
    t0 = stamps[0]
    return sum(
        cf / (1.0 + rate) ** ((ts - t0).total_seconds() / _YEAR_SECONDS) for ts, cf in zip(stamps, amounts, strict=True)
    )


def test_sip_wealth_two_month_path() -> None:
    path = sip_wealth(_TWO_MONTH_STAMPS, _TWO_MONTH_RETS)
    assert path.contribution == MONTHLY_SIP_DOLLARS
    assert path.total_contributed == pytest.approx(_TWO_MONTH_CONTRIBUTED)
    assert path.ending_value == pytest.approx(_TWO_MONTH_END)
    assert path.multiple == pytest.approx(_TWO_MONTH_END / _TWO_MONTH_CONTRIBUTED)
    assert path.max_drawdown == pytest.approx(_TWO_MONTH_MAXDD)
    cash_stamps = [_TWO_MONTH_STAMPS[0], _TWO_MONTH_STAMPS[2], _TWO_MONTH_STAMPS[3]]
    cash_amounts = [-MONTHLY_SIP_DOLLARS, -MONTHLY_SIP_DOLLARS, _TWO_MONTH_END]
    assert _npv(path.cagr, cash_stamps, cash_amounts) == pytest.approx(0.0, abs=1e-8)


def test_sip_cagr_is_xirr_not_time_weighted() -> None:
    path = sip_wealth(_XIRR_STAMPS, _XIRR_RETS)
    assert path.ending_value == pytest.approx(210.0)
    assert path.cagr == pytest.approx(0.10)
    assert path.max_drawdown == pytest.approx(0.0)
    span_years = (_XIRR_STAMPS[-1] - _XIRR_STAMPS[0]).total_seconds() / (365.25 * 24 * 3600)
    twr = (1.0 * 1.10) ** (1.0 / span_years) - 1.0
    assert path.cagr != pytest.approx(twr)


def test_sip_max_drawdown_differs_from_lump_sum() -> None:
    path = sip_wealth(_DIVERGE_STAMPS, _DIVERGE_RETS)
    assert path.max_drawdown == pytest.approx(_DIVERGE_SIP_MAXDD)
    equity = _equity_from_returns(_DIVERGE_STAMPS, _DIVERGE_RETS)
    metrics = compute_metrics(
        run_id="path",
        strategy_name="static",
        equity=equity,
        fills=_empty_fills(),
        ready_bars=0,
        panel_ts=_DIVERGE_STAMPS,
    )
    assert metrics.max_drawdown == pytest.approx(_DIVERGE_LUMP_MAXDD)
    assert metrics.sip_max_drawdown == pytest.approx(_DIVERGE_SIP_MAXDD)
    assert metrics.sip_max_drawdown != pytest.approx(metrics.max_drawdown)


def test_sip_wealth_rejects_length_mismatch() -> None:
    with pytest.raises(ValueError, match="length"):
        sip_wealth(_TWO_MONTH_STAMPS, _TWO_MONTH_RETS[:-1])


def test_compute_metrics_sip_fields_match_two_month_path() -> None:
    equity = _equity_from_returns(_TWO_MONTH_STAMPS, _TWO_MONTH_RETS)
    metrics = compute_metrics(
        run_id="path",
        strategy_name="static",
        equity=equity,
        fills=_empty_fills(),
        ready_bars=0,
        panel_ts=_TWO_MONTH_STAMPS,
    )
    path = sip_wealth(_TWO_MONTH_STAMPS, _TWO_MONTH_RETS)
    assert metrics.sip_contributed == pytest.approx(_TWO_MONTH_CONTRIBUTED)
    assert metrics.sip_value == pytest.approx(_TWO_MONTH_END)
    assert metrics.sip_multiple == pytest.approx(_TWO_MONTH_END / _TWO_MONTH_CONTRIBUTED)
    assert metrics.sip_max_drawdown == pytest.approx(_TWO_MONTH_MAXDD)
    assert metrics.sip_cagr == pytest.approx(path.cagr)
    assert metrics.sip_cagr != pytest.approx(metrics.cagr)
    assert metrics.growth_100 == pytest.approx(100.0 * (1.0 + metrics.total_return))
    table = format_metrics_table([metrics])
    header, row = table.splitlines()[0], table.splitlines()[1]
    assert "CAGR" in header
    assert "$100" in header
    assert "SIP $100/mo" in header
    assert "SIP in" in header
    assert "SIP x" in header
    assert "SIP CAGR" in header
    assert "SIP maxdd" in header
    assert f"{_TWO_MONTH_END:.2f}" in row
    assert f"{_TWO_MONTH_CONTRIBUTED:.2f}" in row
    assert f"{metrics.sip_cagr:.4f}" in row
    assert f"{metrics.sip_max_drawdown:.4f}" in row
