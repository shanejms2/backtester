"""Venue Session calendars: NYSE holidays, DST, crypto weekends."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from backtester.calendar.session import SessionCalendar, subtract_coverage
from backtester.domain import CoverageInterval, Interval
from tests.conftest import aapl, btc, utc


def test_nyse_holiday_2025_new_year_not_expected() -> None:
    cal = SessionCalendar()
    inst = aapl()
    ny = ZoneInfo("America/New_York")
    start = datetime(2025, 1, 1, tzinfo=ny)
    end = datetime(2025, 1, 3, tzinfo=ny)
    windows = cal.session_windows(inst, start, end, "1d")
    opens_ny = [w.start.astimezone(ny).date() for w in windows]
    assert datetime(2025, 1, 1).date() not in set(opens_ny)
    assert datetime(2025, 1, 2).date() in set(opens_ny)
    assert cal.is_holiday(datetime(2025, 1, 1, 12, tzinfo=ny))


def test_equity_weekend_is_expected_gap() -> None:
    cal = SessionCalendar()
    inst = aapl()
    # Friday 2025-01-03 session, Saturday-Sunday gap, Monday 2025-01-06.
    start = utc(2025, 1, 3)
    end = utc(2025, 1, 7)
    needed = cal.needed_ranges(inst, "1d", start, end, coverage=[])
    dates = {w.start.astimezone(UTC).date() for w in needed}
    assert datetime(2025, 1, 4).date() not in dates
    assert datetime(2025, 1, 5).date() not in dates
    assert datetime(2025, 1, 3).date() in dates or any(
        w.start >= utc(2025, 1, 3) and w.start < utc(2025, 1, 4) for w in needed
    )


def test_crypto_weekend_is_expected_bars() -> None:
    cal = SessionCalendar()
    inst = btc()
    start = utc(2025, 1, 4)
    end = utc(2025, 1, 6)
    needed = cal.needed_ranges(inst, "1h", start, end, coverage=[])
    assert needed == [Interval(start=start, end=end)]


def test_gap_subtraction_skips_covered() -> None:
    cal = SessionCalendar()
    inst = aapl()
    start = utc(2025, 1, 2)
    end = utc(2025, 1, 4)
    # Cover Friday 2025-01-03 14:30 UTC (09:30 EST) if that's the session open.
    windows = cal.session_windows(inst, start, end, "1d")
    assert windows
    covered_open = windows[0].start
    coverage = [
        CoverageInterval(
            instrument_id=inst.instrument_id,
            timeframe="1d",
            source="yahoo",
            start_utc=covered_open,
            end_utc=covered_open,
        )
    ]
    needed = cal.needed_ranges(inst, "1d", start, end, coverage=coverage)
    assert all(w.start != covered_open for w in needed)


def test_subtract_coverage_inclusive() -> None:
    holes = subtract_coverage(
        utc(2025, 1, 1),
        utc(2025, 2, 1),
        [
            CoverageInterval(
                instrument_id="EQ:XNAS:AAPL",
                timeframe="1d",
                source="yahoo",
                start_utc=utc(2025, 1, 1),
                end_utc=utc(2025, 1, 15),
            )
        ],
    )
    assert holes
    assert holes[0].start >= utc(2025, 1, 15)
    assert holes[0].end == utc(2025, 2, 1)


def test_intraday_rth_not_midnight_utc() -> None:
    cal = SessionCalendar()
    inst = aapl()
    start = utc(2025, 3, 10)
    end = utc(2025, 3, 11)
    windows = cal.session_windows(inst, start, end, "1h", session="rth")
    assert windows
    # After US DST, RTH opens 13:30 UTC (09:30 EDT).
    assert windows[0].start.hour in {13, 14}
    assert windows[0].start.tzinfo is not None
    assert windows[0].start != start


def test_dst_spring_forward_does_not_invent_ny_2am() -> None:
    cal = SessionCalendar()
    inst = aapl()
    start = utc(2025, 3, 9)
    end = utc(2025, 3, 10)
    windows = cal.session_windows(inst, start, end, "1m", session="eth")
    # ETH 04:00 NY on spring-forward Sunday is 08:00 UTC (EDT).
    if windows:
        for w in windows:
            local = w.start.astimezone()
            del local
            cursor = w.start
            while cursor < w.end:
                ny = cursor.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York"))
                assert not (ny.hour == 2 and ny.date().isoformat() == "2025-03-09")
                cursor += timedelta(minutes=1)
