"""Display TZ: IANA only; reject IST/EDT; DST in NY and Europe."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from backtester.calendar.display import localize, parse_display_tz, utc_offset_minutes
from backtester.exceptions import InvalidTimezoneError


def test_accepts_iana_kolkata() -> None:
    zone = parse_display_tz("Asia/Kolkata")
    assert zone is not None
    assert str(zone) == "Asia/Kolkata"


def test_none_means_utc_only() -> None:
    assert parse_display_tz(None) is None
    assert parse_display_tz("") is None


def test_reject_ist() -> None:
    with pytest.raises(InvalidTimezoneError, match="IANA"):
        parse_display_tz("IST")


def test_reject_edt() -> None:
    with pytest.raises(InvalidTimezoneError, match="IANA"):
        parse_display_tz("EDT")


def test_reject_fixed_offset() -> None:
    with pytest.raises(InvalidTimezoneError, match="fixed offset"):
        parse_display_tz("UTC-4")
    with pytest.raises(InvalidTimezoneError, match="fixed offset"):
        parse_display_tz("GMT+5:30")


def test_unknown_iana_fails_fast() -> None:
    with pytest.raises(InvalidTimezoneError, match="unknown IANA"):
        parse_display_tz("Mars/Olympus")


def test_ny_spring_forward_2025() -> None:
    """2025-03-09 02:00 EST skipped; 07:00 UTC is 03:00 EDT (UTC-4)."""
    ny = ZoneInfo("America/New_York")
    before = datetime(2025, 3, 9, 6, 30, tzinfo=UTC)  # 01:30 EST
    after = datetime(2025, 3, 9, 7, 30, tzinfo=UTC)  # 03:30 EDT
    assert utc_offset_minutes(before, ny) == -5 * 60
    assert utc_offset_minutes(after, ny) == -4 * 60
    local_after = localize(after, ny)
    assert local_after.hour == 3
    assert local_after.tzinfo is not None


def test_ny_fall_back_2025_not_collapsed() -> None:
    """2025-11-02 1:30 happens twice; zone-aware ts_local keeps both offsets."""
    ny = ZoneInfo("America/New_York")
    first = datetime(2025, 11, 2, 5, 30, tzinfo=UTC)  # 01:30 EDT (UTC-4)
    second = datetime(2025, 11, 2, 6, 30, tzinfo=UTC)  # 01:30 EST (UTC-5)
    a = localize(first, ny)
    b = localize(second, ny)
    assert a.hour == 1 and b.hour == 1
    assert a.tzinfo is not None and b.tzinfo is not None
    assert utc_offset_minutes(first, ny) == -4 * 60
    assert utc_offset_minutes(second, ny) == -5 * 60
    assert a.fold != b.fold
    assert a.utcoffset() != b.utcoffset()


def test_london_dst_2025() -> None:
    london = ZoneInfo("Europe/London")
    winter = datetime(2025, 3, 30, 0, 30, tzinfo=UTC)  # still GMT
    summer = datetime(2025, 3, 30, 1, 30, tzinfo=UTC)  # BST
    assert utc_offset_minutes(winter, london) == 0
    assert utc_offset_minutes(summer, london) == 60
    oct_still_bst = datetime(2025, 10, 26, 0, 30, tzinfo=UTC)
    oct_gmt = datetime(2025, 10, 26, 1, 30, tzinfo=UTC)
    assert utc_offset_minutes(oct_still_bst, london) == 60
    assert utc_offset_minutes(oct_gmt, london) == 0
