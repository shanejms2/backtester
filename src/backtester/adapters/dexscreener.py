"""DexScreener adapter: pair identity + PairSnapshots. No candles, no scraping."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx

from backtester.adapters.base import SourceAdapter
from backtester.catalog.instruments import dex_url_parts, looks_like_address
from backtester.domain import Instrument, PairSnapshot, VendorBar
from backtester.exceptions import PermanentVendorError, QuotaError, UnknownInstrumentError
from backtester.normalize.bars import snapshot_quality
from backtester.timeframes import Timeframe

DS_BASE = "https://api.dexscreener.com"
SEARCH_PER_MIN = 60
PAIR_PER_MIN = 300
PAIR_CACHE_S = 30.0


class _TokenBucket:
    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._lock = threading.Lock()
        self._stamps: list[float] = []

    def acquire(self) -> None:
        while True:
            with self._lock:
                cutoff = time.monotonic() - 60.0
                self._stamps = [s for s in self._stamps if s >= cutoff]
                if len(self._stamps) < self.per_minute:
                    self._stamps.append(time.monotonic())
                    return
                wait = 60.0 - (time.monotonic() - self._stamps[0]) + 0.05
            time.sleep(max(wait, 0.05))


class DexScreenerAdapter(SourceAdapter):
    """Public DexScreener REST. Snapshots only — never fake OHLC from priceChange."""

    source_id = "dexscreener"

    def __init__(self, client: httpx.Client | None = None) -> None:
        """Create the adapter with connection reuse."""
        self._client = client or httpx.Client(
            base_url=DS_BASE,
            timeout=20.0,
            headers={"Accept": "application/json", "User-Agent": "backtester-market-data/0.1"},
        )
        self._owns = client is None
        self._search_bucket = _TokenBucket(SEARCH_PER_MIN)
        self._pair_bucket = _TokenBucket(PAIR_PER_MIN)
        self._last_pair: dict[str, float] = {}
        self._truncated = False

    def truncated(self) -> bool:
        """Snapshots have no historical depth."""
        return False

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
        """DexScreener has no candles. Empty iterator; use GeckoTerminal for Bars."""
        del instrument, timeframe, start, end, extended_hours
        return
        yield  # pragma: no cover

    def resolve(self, query: str) -> Instrument:
        """Resolve ticker / mint / ``chain:address`` / DexScreener URL to a DEX Instrument.

        Ticker collisions pick the highest ``liquidity.usd`` pair and keep
        ``alternatives`` on the Catalog record.
        """
        pairs = self._search_pairs(query)
        if not pairs:
            raise UnknownInstrumentError(f"dexscreener found no pairs for {query!r}")
        ranked = sorted(pairs, key=_liquidity, reverse=True)
        best = ranked[0]
        alts = tuple(
            f"DEX:{p.get('chainId')}:{p.get('pairAddress')}"
            for p in ranked[1:]
            if p.get("chainId") and p.get("pairAddress")
        )
        return _instrument_from_pair(best, aliases=(query,), alternatives=alts)

    def fetch_snapshots(self, instrument: Instrument) -> list[PairSnapshot]:
        """Poll one pair. Honors ~30s cache; do not synthesize Bars."""
        chain, pair = instrument.venue, instrument.symbol
        key = f"{chain}:{pair}"
        last = self._last_pair.get(key, 0.0)
        wait = PAIR_CACHE_S - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        self._pair_bucket.acquire()
        payload = self._get(f"/latest/dex/pairs/{chain}/{pair}")
        self._last_pair[key] = time.monotonic()
        pairs = _as_pairs(payload)
        now = datetime.now(UTC)
        return [_snapshot(p, now) for p in pairs]

    def _search_pairs(self, query: str) -> list[dict[str, Any]]:
        text = query.strip()
        url_parts = dex_url_parts(text)
        if url_parts:
            chain, pair = url_parts
            self._pair_bucket.acquire()
            payload = self._get(f"/latest/dex/pairs/{chain}/{pair}")
            return _as_pairs(payload)
        chain_hint, rest = _split_chain_query(text)
        if chain_hint and rest and looks_like_address(rest):
            self._pair_bucket.acquire()
            payload = self._get(f"/latest/dex/pairs/{chain_hint}/{rest}")
            pairs = _as_pairs(payload)
            if pairs:
                return pairs
            self._pair_bucket.acquire()
            payload = self._get(f"/token-pairs/v1/{chain_hint}/{rest}")
            return _as_pairs(payload)
        self._search_bucket.acquire()
        payload = self._get("/latest/dex/search", params={"q": rest or text})
        pairs = _as_pairs(payload)
        if chain_hint:
            pairs = [p for p in pairs if str(p.get("chainId", "")).lower() == chain_hint]
        return pairs

    def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        response = self._client.get(path, params=params)
        if response.status_code == 429:
            raise QuotaError(f"dexscreener HTTP 429 for {path}")
        if response.status_code >= 400:
            raise PermanentVendorError(f"dexscreener HTTP {response.status_code} for {path}")
        return response.json()


def _split_chain_query(text: str) -> tuple[str | None, str]:
    if ":" in text and not text.startswith("http"):
        chain, _, rest = text.partition(":")
        if rest:
            return chain.lower(), rest
    return None, text


def _as_pairs(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if isinstance(payload, dict):
        for key in ("pairs", "pair"):
            val = payload.get(key)
            if isinstance(val, list):
                return [p for p in val if isinstance(p, dict)]
            if isinstance(val, dict):
                return [val]
    return []


def _liquidity(pair: dict[str, Any]) -> float:
    liq = pair.get("liquidity") or {}
    try:
        return float(liq.get("usd") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _ms(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        ms = float(value)
    except (TypeError, ValueError):
        return None
    if ms > 1e12:
        ms /= 1000.0
    return datetime.fromtimestamp(ms, tz=UTC)


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _instrument_from_pair(
    pair: dict[str, Any],
    *,
    aliases: tuple[str, ...],
    alternatives: tuple[str, ...],
) -> Instrument:
    chain = str(pair.get("chainId") or "solana").lower()
    address = str(pair.get("pairAddress") or "")
    base = pair.get("baseToken") or {}
    quote = pair.get("quoteToken") or {}
    ticker = str(base.get("symbol") or address)
    url = pair.get("url")
    extra_aliases = [ticker, address]
    if url:
        extra_aliases.append(str(url))
    created = _ms(pair.get("pairCreatedAt"))
    return Instrument(
        instrument_id=f"DEX:{chain}:{address}",
        asset_class="dex",
        venue=chain,
        symbol=address,
        session_tz="UTC",
        aliases=tuple(dict.fromkeys([*aliases, *extra_aliases])),
        alternatives=alternatives,
        pair_created_at=created,
        base_token=str(base.get("address") or "") or None,
        quote_token=str(quote.get("address") or "") or None,
    )


def _snapshot(pair: dict[str, Any], polled: datetime) -> PairSnapshot:
    base = pair.get("baseToken") or {}
    quote = pair.get("quoteToken") or {}
    volume = pair.get("volume") or {}
    change = pair.get("priceChange") or {}
    liq = _num((pair.get("liquidity") or {}).get("usd"))
    price = _num(pair.get("priceUsd"))
    return PairSnapshot(
        chain_id=str(pair.get("chainId") or ""),
        dex_id=str(pair.get("dexId") or ""),
        pair_address=str(pair.get("pairAddress") or ""),
        base_symbol=str(base.get("symbol") or ""),
        quote_symbol=str(quote.get("symbol") or ""),
        base_address=str(base.get("address") or "") or None,
        quote_address=str(quote.get("address") or "") or None,
        price_usd=price,
        price_native=_num(pair.get("priceNative")),
        liquidity_usd=liq,
        volume_m5=_num(volume.get("m5")),
        volume_h1=_num(volume.get("h1")),
        volume_h6=_num(volume.get("h6")),
        volume_h24=_num(volume.get("h24")),
        price_change_h24=_num(change.get("h24")),
        fdv=_num(pair.get("fdv")),
        market_cap=_num(pair.get("marketCap")),
        pair_created_at=_ms(pair.get("pairCreatedAt")),
        polled_at_utc=polled,
        quality=snapshot_quality(price, liq),
        url=str(pair.get("url") or "") or None,
    )
