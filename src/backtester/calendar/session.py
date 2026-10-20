"""Venue Session calendars: NYSE via exchange_calendars; crypto 24/7 UTC."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from backtester.calendar.display import ensure_utc
from backtester.domain import CoverageInterval, Instrument, Interval, SessionType
from backtester.timeframes import TIMEFRAME_DELTA, Timeframe

NY_TZ = ZoneInfo("America/New_York")
IST = ZoneInfo("Asia/Kolkata")
ETH_OPEN = time(4, 0)
ETH_CLOSE = time(20, 0)
# Default XNYS is ~20y; GLD listed 2004-11-18 and SPY in 1993.
_XNYS_START = "1990-01-01"
_XNSE_START = "1997-01-01"


@lru_cache(maxsize=4)
def _xnys() -> Any:
    import exchange_calendars as xcals

    return xcals.get_calendar("XNYS", start=_XNYS_START)


@lru_cache(maxsize=4)
def _xnse() -> Any:
    import exchange_calendars as xcals

    # Package ships XBOM, not XNSE. BSE/NSE holidays are nearly the same.
    return xcals.get_calendar("XBOM", start=_XNSE_START)


def _as_utc(ts: Any) -> datetime:
    """Convert exchange_calendars / pandas timestamps to aware UTC datetime."""
    if isinstance(ts, datetime):
        return ensure_utc(ts)
    to_py = getattr(ts, "to_pydatetime", None)
    if callable(to_py):
        return ensure_utc(to_py())
    tz = getattr(ts, "tzinfo", None) or getattr(ts, "tz", None)
    year, month, day = int(ts.year), int(ts.month), int(ts.day)
    hour = int(getattr(ts, "hour", 0))
    minute = int(getattr(ts, "minute", 0))
    second = int(getattr(ts, "second", 0))
    dt = datetime(year, month, day, hour, minute, second, tzinfo=tz)
    return ensure_utc(dt)


class SessionCalendar:
    """When an Instrument is expected to print bars.

    US equity Sessions follow XNYS (holidays, early closes, DST). Crypto and DEX
    are 24/7 UTC. Display timezone is not used here.
    """

    def session_windows(
        self,
        instrument: Instrument,
        start: datetime,
        end: datetime,
        timeframe: Timeframe,
        session: SessionType = "rth",
    ) -> list[Interval]:
        """UTC intervals during which bars should exist.

        Args:
            instrument: Series whose venue clock applies.
            start: Inclusive UTC start of the Request.
            end: Exclusive UTC end of the Request (or inclusive end + 1ns treated as range).
            timeframe: Native Timeframe (daily windows collapse to session open).
            session: ``rth`` (09:30–16:00 NY) or ``eth`` (04:00–20:00 NY). Ignored for crypto.

        Returns:
            Sorted, non-overlapping UTC intervals.
        """
        start_utc = ensure_utc(start)
        end_utc = ensure_utc(end)
        if end_utc <= start_utc:
            return []
        if instrument.asset_class in {"crypto", "dex"} or instrument.venue.upper() == "CCY":
            return [Interval(start=start_utc, end=end_utc)]
        if instrument.venue.upper() == "XNSE":
            return self._listed_windows(_xnse(), IST, start_utc, end_utc, timeframe, session)
        return self._listed_windows(_xnys(), NY_TZ, start_utc, end_utc, timeframe, session)

    def session_dates(self, start: datetime, end: datetime) -> list[date]:
        """NYSE session dates whose RTH overlaps ``[start, end)``.

        Crypto weekend bars are ignored: the spine is XNYS only. Bounds are
        UTC; the returned dates are America/New_York session dates.
        """
        start_utc = ensure_utc(start)
        end_utc = ensure_utc(end)
        if end_utc <= start_utc:
            return []
        cal = _xnys()
        start_ny = start_utc.astimezone(NY_TZ).date()
        end_ny = (end_utc - timedelta(microseconds=1)).astimezone(NY_TZ).date()
        sessions = cal.sessions_in_range(str(start_ny), str(end_ny))
        out: list[date] = []
        for label in sessions:
            open_ts = _as_utc(cal.session_open(label))
            close_ts = _as_utc(cal.session_close(label))
            if open_ts < end_utc and close_ts > start_utc:
                out.append(open_ts.astimezone(NY_TZ).date())
        return out

    def xnse_session_dates(self, start: datetime, end: datetime) -> list[date]:
        """NSE session dates (XBOM calendar) whose session overlaps ``[start, end)``."""
        start_utc = ensure_utc(start)
        end_utc = ensure_utc(end)
        if end_utc <= start_utc:
            return []
        cal = _xnse()
        start_ist = start_utc.astimezone(IST).date()
        end_ist = (end_utc - timedelta(microseconds=1)).astimezone(IST).date()
        sessions = cal.sessions_in_range(str(start_ist), str(end_ist))
        out: list[date] = []
        for label in sessions:
            open_ts = _as_utc(cal.session_open(label))
            close_ts = _as_utc(cal.session_close(label))
            if open_ts < end_utc and close_ts > start_utc:
                out.append(open_ts.astimezone(IST).date())
        return out

    def is_holiday(self, day: datetime) -> bool:
        """Return True if ``day`` (any tz) is a full NYSE holiday."""
        cal = _xnys()
        ny_date = ensure_utc(day).astimezone(NY_TZ).date()
        is_session = getattr(cal, "is_session", None)
        if callable(is_session):
            return not bool(is_session(str(ny_date)))
        sessions = cal.sessions_in_range(str(ny_date), str(ny_date))
        return len(sessions) == 0

    def needed_ranges(
        self,
        instrument: Instrument,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        coverage: list[CoverageInterval],
        session: SessionType = "rth",
    ) -> list[Interval]:
        """Coverage-first holes intersected with expected Sessions.

        Subtracts inclusive Coverage from ``[start, end)``, drops weekends /
        holidays / overnight for equity, and keeps crypto weekends.
        """
        start_utc = ensure_utc(start)
        end_utc = ensure_utc(end)
        holes = subtract_coverage(start_utc, end_utc, coverage)
        windows = self.session_windows(instrument, start_utc, end_utc, timeframe, session)
        needed: list[Interval] = []
        for hole in holes:
            for window in windows:
                clipped = intersect(hole, window)
                if clipped is not None:
                    needed.append(clipped)
        return merge_intervals(needed)

    def _listed_windows(
        self,
        cal: Any,
        zone: ZoneInfo,
        start_utc: datetime,
        end_utc: datetime,
        timeframe: Timeframe,
        session: SessionType,
    ) -> list[Interval]:
        start_local = start_utc.astimezone(zone).date()
        end_local = (end_utc - timedelta(microseconds=1)).astimezone(zone).date()
        sessions = cal.sessions_in_range(str(start_local), str(end_local))
        out: list[Interval] = []
        for label in sessions:
            rth_open = _as_utc(cal.session_open(label))
            rth_close = _as_utc(cal.session_close(label))
            if session == "eth" and zone is NY_TZ:
                ny_date = rth_open.astimezone(NY_TZ).date()
                open_ts = datetime.combine(ny_date, ETH_OPEN, tzinfo=NY_TZ).astimezone(UTC)
                close_ts = datetime.combine(ny_date, ETH_CLOSE, tzinfo=NY_TZ).astimezone(UTC)
                if rth_close < close_ts:
                    close_ts = rth_close
            else:
                open_ts, close_ts = rth_open, rth_close
            if timeframe == "1d":
                bar_end = open_ts + TIMEFRAME_DELTA["1d"]
                if start_utc <= open_ts < end_utc:
                    out.append(Interval(start=open_ts, end=bar_end))
                continue
            window = Interval(start=open_ts, end=close_ts)
            clipped = intersect(window, Interval(start=start_utc, end=end_utc))
            if clipped is not None:
                out.append(clipped)
        return out


def subtract_coverage(
    start: datetime,
    end: datetime,
    coverage: list[CoverageInterval],
) -> list[Interval]:
    """Return ``[start, end)`` minus inclusive Coverage ranges."""
    holes = [Interval(start=start, end=end)]
    for cov in sorted(coverage, key=lambda c: c.start_utc):
        cov_iv = Interval(start=ensure_utc(cov.start_utc), end=ensure_utc(cov.end_utc))
        # Coverage is inclusive of end_utc (last bar open). Treat as [start, next).
        next_end = cov_iv.end + timedelta(microseconds=1)
        cov_half = Interval(start=cov_iv.start, end=max(cov_iv.start, next_end))
        holes = _subtract_one(holes, cov_half)
    return [h for h in holes if h.end > h.start]


def intersect(a: Interval, b: Interval) -> Interval | None:
    """Return the overlap of two intervals, or None."""
    start = max(a.start, b.start)
    end = min(a.end, b.end)
    if end > start:
        return Interval(start=start, end=end)
    return None


def merge_intervals(intervals: list[Interval]) -> list[Interval]:
    """Merge overlapping or touching UTC intervals."""
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda i: i.start)
    merged = [ordered[0]]
    for current in ordered[1:]:
        last = merged[-1]
        if current.start <= last.end:
            merged[-1] = Interval(start=last.start, end=max(last.end, current.end))
        else:
            merged.append(current)
    return merged


def _subtract_one(holes: list[Interval], cut: Interval) -> list[Interval]:
    out: list[Interval] = []
    for hole in holes:
        if cut.end <= hole.start or cut.start >= hole.end:
            out.append(hole)
            continue
        if cut.start > hole.start:
            out.append(Interval(start=hole.start, end=min(cut.start, hole.end)))
        if cut.end < hole.end:
            out.append(Interval(start=max(cut.end, hole.start), end=hole.end))
    return out
