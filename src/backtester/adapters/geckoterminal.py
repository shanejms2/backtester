"""GeckoTerminal adapter: DEX pool OHLCV. DexScreener cannot supply candles."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx

from backtester.adapters.base import SourceAdapter
from backtester.calendar.display import ensure_utc
from backtester.domain import Instrument, VendorBar
from backtester.exceptions import PermanentVendorError, QuotaError
from backtester.timeframes import Timeframe

GT_BASE = "https://api.geckoterminal.com/api/v2"
DS_TO_GECKO = {
    "solana": "solana",
    "ethereum": "eth",
    "eth": "eth",
    "base": "base",
    "arbitrum": "arbitrum",
    "bsc": "bsc",
    "polygon": "polygon_pos",
    "avalanche": "avax",
    "optimism": "optimism",
    "hyperliquid": "hyperevm",
    "hyperevm": "hyperevm",
    "robinhood": "robinhood",
}

TF_MAP: dict[Timeframe, tuple[str, str]] = {
    "1m": ("minute", "1"),
    "5m": ("minute", "5"),
    "15m": ("minute", "15"),
    "1h": ("hour", "1"),
    "4h": ("hour", "4"),
    "1d": ("day", "1"),
}


class GeckoTerminalAdapter(SourceAdapter):
    """``GET /networks/{network}/pools/{pool}/ohlcv/{timeframe}`` with ``before_timestamp``."""

    source_id = "geckoterminal"

    def __init__(self, client: httpx.Client | None = None) -> None:
        """Create the adapter with connection reuse."""
        self._client = client or httpx.Client(
            base_url=GT_BASE,
            timeout=25.0,
            headers={"Accept": "application/json", "User-Agent": "backtester-market-data/0.1"},
        )
        self._owns = client is None
        self._truncated = False

    def truncated(self) -> bool:
        """True when public history (~180d) stopped the page loop."""
        return self._truncated

    def close(self) -> None:
        """Close the owned client."""
        if self._owns:
            self._client.close()

    def fetch_pages(
        self,
        instrument: Instrument,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        *,
        extended_hours: bool = False,
    ) -> Iterator[list[VendorBar]]:
        """Paginate backwards with ``before_timestamp``. Stop on 401/empty without covering older holes."""
        del extended_hours
        if timeframe not in TF_MAP:
            return
        gecko_tf, aggregate = TF_MAP[timeframe]
        network = DS_TO_GECKO.get(instrument.venue.lower(), instrument.venue.lower())
        pool = instrument.symbol
        start_utc = ensure_utc(start)
        end_utc = ensure_utc(end)
        before: int | None = int(end_utc.timestamp())
        self._truncated = False
        seen: set[int] = set()
        for _ in range(16):
            params = {
                "aggregate": aggregate,
                "token": "base",
                "limit": "1000",
            }
            if before is not None:
                params["before_timestamp"] = str(before)
            try:
                payload = self._get(f"/networks/{network}/pools/{pool}/ohlcv/{gecko_tf}", params)
            except PermanentVendorError as exc:
                if "401" in str(exc):
                    self._truncated = True
                    return
                raise
            chunk = ((payload.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
            if not chunk:
                if before is not None and before > int(start_utc.timestamp()):
                    self._truncated = True
                return
            page: list[VendorBar] = []
            oldest = int(min(r[0] for r in chunk))
            for row in chunk:
                ts = int(row[0])
                if ts in seen:
                    continue
                seen.add(ts)
                stamp = datetime.fromtimestamp(ts, tz=UTC)
                if stamp < start_utc or stamp >= end_utc:
                    continue
                page.append(
                    VendorBar(
                        source=self.source_id,
                        vendor_symbol=pool,
                        timeframe=timeframe,
                        ts=stamp,
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[5]),
                        ts_kind="utc",
                    )
                )
            if page:
                yield page
            if oldest <= int(start_utc.timestamp()):
                return
            if len(chunk) < 50:
                if oldest > int(start_utc.timestamp()):
                    self._truncated = True
                return
            before = oldest
        self._truncated = True

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        response = self._client.get(path, params=params)
        if response.status_code == 429:
            raise QuotaError(f"geckoterminal HTTP 429 for {path}")
        if response.status_code == 401:
            raise PermanentVendorError(f"geckoterminal HTTP 401 for {path}")
        if response.status_code >= 400:
            raise PermanentVendorError(f"geckoterminal HTTP {response.status_code} for {path}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise PermanentVendorError("geckoterminal returned non-object JSON")
        return payload
