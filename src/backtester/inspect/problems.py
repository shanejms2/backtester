"""Unexpected Gaps, quality flags, and spike markers. Pure functions; the test surface."""

from __future__ import annotations

from datetime import datetime, timedelta

import polars as pl

from backtester.calendar.display import ensure_utc
from backtester.calendar.session import SessionCalendar
from backtester.domain import Instrument, Interval, SessionType
from backtester.timeframes import TIMEFRAME_DELTA, Timeframe

SPIKE_WINDOW = 21
SPIKE_MULT = 8.0


class BarProblem:
    """One visual flag on a Chart.

    Attributes:
        kind: ``gap``, ``suspect``, ``partial``, ``zero_volume``, or ``spike``.
        ts_utc: Start of the flag (bar open, or gap start).
        end_utc: Gap end (exclusive of the next present bar). Same as ``ts_utc`` for point flags.
        label: Short reason for the Chart hover.
    """

    __slots__ = ("kind", "ts_utc", "end_utc", "label")

    def __init__(self, kind: str, ts_utc: datetime, end_utc: datetime, label: str) -> None:
        self.kind = kind
        self.ts_utc = ensure_utc(ts_utc)
        self.end_utc = ensure_utc(end_utc)
        self.label = label


def find_problems(
    frame: pl.DataFrame,
    instrument: Instrument,
    timeframe: Timeframe,
    *,
    calendar: SessionCalendar | None = None,
    session: SessionType = "rth",
) -> list[BarProblem]:
    """Detect unexpected Gaps and quality/spike flags on stored Bars.

    Expected Gaps (equity weekend, NYSE holiday) are omitted. Crypto weekends
    are expected bars, so a missing Saturday is an unexpected Gap.

    Args:
        frame: Canonical Bars with ``ts_utc``, OHLC, ``volume``, ``quality``, ``is_partial``.
        instrument: Venue Session owner.
        timeframe: Native stored Timeframe.
        calendar: Session calendar. Created when omitted.
        session: ``rth`` or ``eth`` for equities.

    Returns:
        Sorted flags. Gap rows are merged into contiguous spans.
    """
    if frame.is_empty():
        return []
    cal = calendar or SessionCalendar()
    stamps = [ensure_utc(ts) for ts in frame["ts_utc"].to_list()]
    present = set(stamps)
    first, last = min(stamps), max(stamps)
    expected = expected_bar_opens(instrument, timeframe, first, last, cal, session=session)
    missing = [ts for ts in expected if ts not in present]
    problems: list[BarProblem] = _merge_missing(missing, timeframe)
    problems.extend(_point_flags(frame))
    problems.extend(_spike_flags(frame))
    problems.sort(key=lambda p: (p.ts_utc, p.kind))
    return problems


def expected_bar_opens(
    instrument: Instrument,
    timeframe: Timeframe,
    first: datetime,
    last: datetime,
    calendar: SessionCalendar,
    *,
    session: SessionType = "rth",
) -> list[datetime]:
    """Bar-open UTC stamps that should exist between ``first`` and ``last`` inclusive."""
    first_utc = ensure_utc(first)
    last_utc = ensure_utc(last)
    delta = TIMEFRAME_DELTA[timeframe]
    windows = calendar.session_windows(
        instrument,
        first_utc,
        last_utc + timedelta(microseconds=1),
        timeframe,
        session,
    )
    opens: list[datetime] = []
    for window in windows:
        ts = window.start
        if ts < first_utc:
            steps = (first_utc - ts) // delta
            ts = ts + steps * delta
            if ts < first_utc:
                ts += delta
        while ts <= last_utc and ts < window.end:
            opens.append(ts)
            ts += delta
    return opens


def _merge_missing(missing: list[datetime], timeframe: Timeframe) -> list[BarProblem]:
    if not missing:
        return []
    delta = TIMEFRAME_DELTA[timeframe]
    ordered = sorted(missing)
    spans: list[BarProblem] = []
    start = ordered[0]
    prev = ordered[0]
    for ts in ordered[1:]:
        if ts == prev + delta:
            prev = ts
            continue
        spans.append(BarProblem("gap", start, prev + delta, "unexpected gap"))
        start = ts
        prev = ts
    spans.append(BarProblem("gap", start, prev + delta, "unexpected gap"))
    return spans


def _point_flags(frame: pl.DataFrame) -> list[BarProblem]:
    out: list[BarProblem] = []
    for row in frame.iter_rows(named=True):
        ts = ensure_utc(row["ts_utc"])
        quality = str(row.get("quality") or "ok")
        if quality == "suspect":
            out.append(BarProblem("suspect", ts, ts, "quality=suspect"))
        if bool(row.get("is_partial")):
            out.append(BarProblem("partial", ts, ts, "is_partial"))
        volume = float(row["volume"])
        if volume <= 0:
            out.append(BarProblem("zero_volume", ts, ts, "volume<=0"))
    return out


def _spike_flags(frame: pl.DataFrame) -> list[BarProblem]:
    if frame.height < SPIKE_WINDOW:
        return []
    ranges = frame.select(
        pl.col("ts_utc"),
        (pl.col("high") - pl.col("low")).alias("bar_range"),
    ).with_columns(
        pl.col("bar_range").rolling_median(window_size=SPIKE_WINDOW, min_samples=5).alias("med_range"),
    )
    out: list[BarProblem] = []
    for row in ranges.iter_rows(named=True):
        med = row["med_range"]
        bar_range = row["bar_range"]
        if med is None or med <= 0 or bar_range is None:
            continue
        if float(bar_range) > SPIKE_MULT * float(med):
            ts = ensure_utc(row["ts_utc"])
            out.append(BarProblem("spike", ts, ts, f"range {SPIKE_MULT:g}x rolling median"))
    return out


def gap_intervals(problems: list[BarProblem]) -> list[Interval]:
    """Unexpected Gap spans for Chart shapes."""
    return [Interval(start=p.ts_utc, end=p.end_utc) for p in problems if p.kind == "gap"]
