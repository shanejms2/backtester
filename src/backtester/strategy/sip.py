"""Monthly SIP overlay on a Metrics return series. Not a Broker path."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

MONTHLY_SIP_DOLLARS = 100.0
_YEAR_SECONDS = 365.0 * 24.0 * 3600.0


@dataclass(frozen=True)
class SipPath:
    """Wealth path from monthly contributions on a return series.

    Attributes:
        contribution: Dollars added on each SIP day.
        total_contributed: Sum of contributions in the window.
        ending_value: Wealth at the last bar.
        multiple: ``ending_value / total_contributed`` (0 if nothing was contributed).
        max_drawdown: Peak-to-trough of the post-contribution wealth series.
        cagr: Money-weighted XIRR of SIP cashflows (not time-weighted CAGR).
    """

    contribution: float
    total_contributed: float
    ending_value: float
    multiple: float
    max_drawdown: float
    cagr: float


def sip_wealth(
    timestamps: Sequence[datetime],
    returns: Sequence[float],
    *,
    contribution: float = MONTHLY_SIP_DOLLARS,
) -> SipPath:
    """Contribute on the first decision day of each calendar month.

    ``wealth_t = wealth_{t-1} * (1 + r_t) + contribution_t``. Contribution is
    zero except on SIP days — the first timestamp of each ``(year, month)`` in
    the series (UTC calendar on stored bars). Initial wealth is 0, so the first
    bar's return does not mark new cash. That matches lump-sum ``growth_100``,
    which also starts from ready-bar equity after that bar's return.

    ``cagr`` is the XIRR of dated cashflows: ``-contribution`` on each SIP day
    and ``+ending_value`` on the last bar (same-day flows net). That is not
    the time-weighted CAGR of ``r_t``, which equals lump-sum CAGR.

    Args:
        timestamps: Decision-day stamps aligned with ``returns`` (Metrics window).
        returns: Per-bar portfolio returns ``r_t`` on those stamps.
        contribution: Dollars added on each SIP day. Defaults to $100.

    Returns:
        Totals, peak-to-trough drawdown, and money-weighted CAGR for the SIP path.

    Raises:
        ValueError: If ``timestamps`` and ``returns`` differ in length.
    """
    if len(timestamps) != len(returns):
        raise ValueError(f"timestamps length {len(timestamps)} != returns length {len(returns)}")
    wealth = 0.0
    total = 0.0
    prev_month: tuple[int, int] | None = None
    ordered = sorted(zip(timestamps, returns, strict=True), key=lambda item: item[0])
    wealths: list[float] = []
    sip_days: list[datetime] = []
    last_ts: datetime | None = None
    for ts, ret in ordered:
        month = (ts.year, ts.month)
        add = contribution if month != prev_month else 0.0
        if add:
            total += add
            prev_month = month
            sip_days.append(ts)
        wealth = wealth * (1.0 + ret) + add
        wealths.append(wealth)
        last_ts = ts
    multiple = wealth / total if total else 0.0
    return SipPath(
        contribution=contribution,
        total_contributed=total,
        ending_value=wealth,
        multiple=multiple,
        max_drawdown=_max_drawdown(wealths),
        cagr=_sip_xirr(sip_days, last_ts, contribution, wealth),
    )


def _max_drawdown(wealths: Sequence[float]) -> float:
    """Peak-to-trough decline on post-contribution wealth (0 if never positive)."""
    peak = 0.0
    max_dd = 0.0
    for wealth in wealths:
        if wealth > peak:
            peak = wealth
        if peak > 0.0:
            drawdown = wealth / peak - 1.0
            if drawdown < max_dd:
                max_dd = drawdown
    return max_dd


def _sip_xirr(
    sip_days: Sequence[datetime],
    last_ts: datetime | None,
    contribution: float,
    ending_value: float,
) -> float:
    """XIRR of ``-contribution`` on each SIP day and ``+ending_value`` at the end."""
    if last_ts is None:
        return 0.0
    by_ts: dict[datetime, float] = {}
    for ts in sip_days:
        by_ts[ts] = by_ts.get(ts, 0.0) - contribution
    by_ts[last_ts] = by_ts.get(last_ts, 0.0) + ending_value
    cashflows = sorted(((ts, amount) for ts, amount in by_ts.items() if amount != 0.0), key=lambda item: item[0])
    return _xirr(cashflows)


def _xirr(cashflows: Sequence[tuple[datetime, float]]) -> float:
    """Money-weighted annual return (Excel-style XIRR, 365-day year)."""
    if len(cashflows) < 2:
        return 0.0
    t0 = cashflows[0][0]
    times = [(ts - t0).total_seconds() / _YEAR_SECONDS for ts, _ in cashflows]
    amounts = [amount for _, amount in cashflows]
    if times[-1] <= 0.0:
        return 0.0

    def npv(rate: float) -> float:
        base = 1.0 + rate
        if base <= 0.0:
            return float("inf")
        total = 0.0
        for cf, t in zip(amounts, times, strict=True):
            total += cf / base**t
        return total

    low = -0.999999
    high = 1.0
    npv_high = npv(high)
    while npv_high > 0.0 and high < 1e12:
        high *= 2.0
        npv_high = npv(high)
    npv_low = npv(low)
    if npv_low * npv_high > 0.0:
        return 0.0
    for _ in range(128):
        mid = (low + high) / 2.0
        value = npv(mid)
        if abs(value) < 1e-12:
            return mid
        if npv_low * value > 0.0:
            low = mid
            npv_low = value
        else:
            high = mid
    return (low + high) / 2.0
