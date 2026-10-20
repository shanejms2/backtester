"""Read-only JSON routes over BarReader / catalog / coverage."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from backtester.api.deps import SettingsDep, StoreDep, enforce_api_key
from backtester.api.serialize import (
    bars_frame_to_json,
    coverage_to_json,
    ensure_display_tz,
    instrument_to_json,
    parse_range,
)
from backtester.catalog.instruments import InstrumentCatalog
from backtester.exceptions import BacktesterError, InvalidTimezoneError
from backtester.query.reader import BarReader
from backtester.timeframes import parse_timeframe

router = APIRouter(prefix="/v1", dependencies=[Depends(enforce_api_key)])


@router.get("/instruments")
def get_instrument(
    store: StoreDep,
    q: str = Query(..., min_length=1, description="Stable id, ticker, or registered alias."),
) -> dict[str, Any]:
    """Look up a registered instrument. Does not synthesize new catalog rows."""
    catalog = InstrumentCatalog(store)
    query = q.strip()
    found = catalog.get(query) or store.find_by_alias(query)
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown instrument {query!r}")
    return instrument_to_json(found)


@router.get("/coverage")
def get_coverage(
    store: StoreDep,
    instrument_id: str | None = Query(default=None),
    tf: str | None = Query(default=None, description="Native timeframe when instrument_id is set."),
    source: str | None = Query(default=None),
) -> dict[str, Any]:
    """Return coverage intervals. Omit instrument_id for the full inventory."""
    if instrument_id is None:
        items = store.list_coverage()
        if source:
            items = [c for c in items if c.source == source]
        if tf:
            timeframe = parse_timeframe(tf)
            items = [c for c in items if c.timeframe == timeframe]
        return {"coverage": [coverage_to_json(c) for c in items]}

    if not tf:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="tf is required when instrument_id is set",
        )
    timeframe = parse_timeframe(tf)
    if source:
        ranges = store.coverage(instrument_id, timeframe, source)
    else:
        ranges = [c for c in store.list_coverage() if c.instrument_id == instrument_id and c.timeframe == timeframe]
    return {"coverage": [coverage_to_json(c) for c in ranges]}


@router.get("/bars")
def get_bars(
    store: StoreDep,
    settings: SettingsDep,
    instrument_id: str = Query(...),
    tf: str = Query(..., description="Native stored timeframe."),
    start: str = Query(..., alias="from"),
    end: str | None = Query(default=None, alias="to"),
    source: str | None = Query(default=None),
    tz: str | None = Query(default=None, description="IANA display zone; adds ts_local."),
    resample: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=100_000),
) -> dict[str, Any]:
    """Return stored OHLC bars as a JSON array (read-only; never fetches)."""
    try:
        timeframe = parse_timeframe(tf)
        display_tz = ensure_display_tz(tz, settings.display_tz)
        start_dt, end_dt = parse_range(start, end, display_tz)
    except InvalidTimezoneError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except (ValueError, BacktesterError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    max_bars = settings.api_max_bars
    row_limit = min(limit or max_bars, max_bars)
    reader = BarReader(store)
    try:
        frame = reader.read(
            instrument_id,
            timeframe,
            start_dt,
            end_dt,
            tz=display_tz,
            resample=resample,
            source=source,
        ).collect()
    except BacktesterError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    truncated = frame.height > row_limit
    if truncated:
        frame = frame.head(row_limit)
    return {
        "instrument_id": instrument_id,
        "timeframe": resample or timeframe,
        "source": source,
        "tz": display_tz,
        "from": start_dt.isoformat().replace("+00:00", "Z"),
        "to": end_dt.isoformat().replace("+00:00", "Z"),
        "count": frame.height,
        "truncated": truncated,
        "limit": row_limit,
        "bars": bars_frame_to_json(frame),
    }
