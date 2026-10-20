"""JSON-friendly shapes for instruments, coverage, and bars."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from backtester.calendar.display import require_zone
from backtester.domain import CoverageInterval, Instrument


def instrument_to_json(instrument: Instrument) -> dict[str, Any]:
    return {
        "instrument_id": instrument.instrument_id,
        "asset_class": instrument.asset_class,
        "venue": instrument.venue,
        "symbol": instrument.symbol,
        "session_tz": instrument.session_tz,
        "aliases": list(instrument.aliases),
        "alternatives": list(instrument.alternatives),
        "pair_created_at": _iso(instrument.pair_created_at),
        "base_token": instrument.base_token,
        "quote_token": instrument.quote_token,
    }


def coverage_to_json(item: CoverageInterval) -> dict[str, Any]:
    return {
        "instrument_id": item.instrument_id,
        "timeframe": item.timeframe,
        "source": item.source,
        "start_utc": _iso(item.start_utc),
        "end_utc": _iso(item.end_utc),
    }


def bars_frame_to_json(frame: pl.DataFrame) -> list[dict[str, Any]]:
    if frame.is_empty():
        return []
    rows: list[dict[str, Any]] = []
    for row in frame.iter_rows(named=True):
        item: dict[str, Any] = {}
        for key, value in row.items():
            if isinstance(value, datetime):
                item[key] = _iso(value)
            elif value is None:
                item[key] = None
            elif isinstance(value, (bool, int, float, str)):
                item[key] = value
            else:
                item[key] = str(value)
        rows.append(item)
    return rows


def parse_range(start_raw: str, end_raw: str | None, tz: str) -> tuple[datetime, datetime]:
    start = parse_dt(start_raw, tz, is_end=False)
    end = parse_dt(end_raw, tz, is_end=True) if end_raw else datetime.now(UTC)
    return start, end


def parse_dt(raw: str, tz: str, *, is_end: bool) -> datetime:
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    date_only = len(text) == 10
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = datetime.strptime(text, "%Y-%m-%d")
        date_only = True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=require_zone(tz))
    if date_only and is_end:
        parsed = parsed + timedelta(days=1)
    return parsed


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat().replace("+00:00", "Z")


def ensure_display_tz(tz: str | None, default: str) -> str:
    name = (tz or default or "UTC").strip() or "UTC"
    require_zone(name)
    # Touch ZoneInfo so bad names fail the same way as require_zone.
    ZoneInfo(name)
    return name
