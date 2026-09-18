"""Display timezone: IANA only, fail fast, never stored."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backtester.exceptions import InvalidTimezoneError

# Ambiguous civil abbreviations. IST is India / Ireland / Israel.
_ABBREVIATIONS = frozenset(
    {
        "IST",
        "EST",
        "EDT",
        "PST",
        "PDT",
        "CST",
        "CDT",
        "MST",
        "MDT",
        "GMT",
        "BST",
        "CET",
        "CEST",
        "EET",
        "EEST",
        "WET",
        "WEST",
        "JST",
        "KST",
        "HKT",
        "SGT",
        "AEST",
        "AEDT",
        "AKST",
        "AKDT",
        "HST",
        "AST",
        "ADT",
        "NST",
        "NDT",
        "IDT",
        "NZST",
        "NZDT",
        "MSK",
        "WIB",
        "WITA",
        "WIT",
    }
)

_OFFSET_RE = re.compile(
    r"^(?:UTC|GMT)?[+-]\d{1,2}(?::?\d{2})?$",
    re.IGNORECASE,
)


def parse_display_tz(name: str | None) -> ZoneInfo | None:
    """Parse an IANA display zone.

    Args:
        name: IANA name such as ``Asia/Kolkata``, ``UTC``, or ``None`` (UTC-only read).

    Returns:
        ``ZoneInfo`` when ``name`` is set, otherwise ``None``.

    Raises:
        InvalidTimezoneError: Abbreviation, fixed offset, or unknown IANA name.
    """
    if name is None or name.strip() == "":
        return None
    raw = name.strip()
    upper = raw.upper()
    if upper in _ABBREVIATIONS:
        hint = _hint(upper)
        raise InvalidTimezoneError(f"{raw!r} is an ambiguous abbreviation; pass an IANA name{hint}.")
    compact = raw.replace(" ", "")
    if _OFFSET_RE.match(compact) and upper not in {"UTC", "GMT"}:
        raise InvalidTimezoneError(
            f"{raw!r} is a fixed offset; pass an IANA name such as 'Asia/Kolkata' or 'America/New_York'."
        )
    if upper == "GMT":
        raise InvalidTimezoneError("'GMT' is an abbreviation; pass IANA 'UTC' or 'Europe/London'.")
    try:
        return ZoneInfo(raw)
    except ZoneInfoNotFoundError as exc:
        raise InvalidTimezoneError(
            f"unknown IANA timezone {raw!r}; do not fall back. Example: 'Asia/Kolkata'."
        ) from exc


def require_zone(name: str) -> ZoneInfo:
    """Parse a required IANA zone (CLI ``--tz``, request ranges).

    Args:
        name: IANA name. Empty is treated as UTC.

    Returns:
        Resolved ``ZoneInfo``.
    """
    zone = parse_display_tz(name) if name.strip() else ZoneInfo("UTC")
    return zone if zone is not None else ZoneInfo("UTC")


def ensure_utc(ts: datetime) -> datetime:
    """Return ``ts`` as timezone-aware UTC.

    Naive datetimes are treated as already-UTC (storage invariant callers).
    """
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def to_utc(ts: datetime, zone: ZoneInfo) -> datetime:
    """Interpret ``ts`` in ``zone`` when naive, then convert to UTC."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=zone)
    return ts.astimezone(UTC)


def localize(ts_utc: datetime, zone: ZoneInfo) -> datetime:
    """Project a UTC timestamp into ``zone`` (zone-aware, not naive)."""
    return ensure_utc(ts_utc).astimezone(zone)


def utc_offset_minutes(ts_utc: datetime, zone: ZoneInfo) -> int:
    """UTC offset of ``ts_utc`` as displayed in ``zone``, in minutes (DST-aware)."""
    local = localize(ts_utc, zone)
    offset = local.utcoffset()
    if offset is None:
        return 0
    return int(offset.total_seconds() // 60)


def _hint(abbr: str) -> str:
    hints = {
        "IST": " (India: 'Asia/Kolkata'; Ireland: 'Europe/Dublin'; Israel: 'Asia/Jerusalem')",
        "EST": " such as 'America/New_York'",
        "EDT": " such as 'America/New_York'",
        "PST": " such as 'America/Los_Angeles'",
        "PDT": " such as 'America/Los_Angeles'",
        "GMT": " such as 'UTC' or 'Europe/London'",
    }
    return hints.get(abbr, " such as 'America/New_York' or 'Asia/Kolkata'")
