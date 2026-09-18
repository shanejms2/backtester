"""Instrument identity: stable ids, aliases, ticker → id. Not vendor protocol."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Protocol

from backtester.calendar.display import ensure_utc
from backtester.domain import AssetClass, Instrument
from backtester.exceptions import UnknownInstrumentError

_PAIR_URL = re.compile(
    r"https?://(?:www\.)?dexscreener\.com/([^/]+)/([^/?#]+)",
    re.IGNORECASE,
)
_EVM = re.compile(r"^0x[a-fA-F0-9]{40}$")
_SOL = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_ID = re.compile(r"^(EQ|CRYPTO|DEX):", re.IGNORECASE)

# Tiny MIC hints. Unknown US tickers default to XNAS (plan layout uses XNAS:AAPL).
_NYSE_TICKERS = frozenset(
    {
        "IBM",
        "GE",
        "BA",
        "DIS",
        "KO",
        "WMT",
        "JPM",
        "JNJ",
        "PG",
        "V",
        "MA",
        "XOM",
        "CVX",
        "CAT",
        "MMM",
        "MCD",
        "NKE",
        "HD",
        "UNH",
        "WFC",
        "GS",
        "T",
        "VZ",
        "PFE",
        "MRK",
        "C",
        "BAC",
    }
)


class InstrumentStore(Protocol):
    """Persistence for Catalog records. Implemented by the Store adapter."""

    def upsert_instrument(self, instrument: Instrument) -> None:
        """Insert or replace an Instrument and its aliases."""

    def get_instrument(self, instrument_id: str) -> Instrument | None:
        """Return the Instrument for ``instrument_id``, if known."""

    def find_by_alias(self, alias: str) -> Instrument | None:
        """Return the Instrument registered under ``alias`` (case-insensitive)."""


def parse_instrument_id(instrument_id: str) -> tuple[AssetClass, str, str]:
    """Split a stable id into ``(asset_class, venue, symbol)``.

    Crypto symbols may contain extra colons (CCXT perps ``BTC/USDT:USDT``).
    """
    parts = instrument_id.split(":", 2)
    if len(parts) < 3:
        raise UnknownInstrumentError(f"malformed instrument id {instrument_id!r}")
    kind, venue, symbol = parts[0].upper(), parts[1], parts[2]
    if kind == "EQ":
        return "equity", venue.upper(), symbol.upper()
    if kind == "CRYPTO":
        return "crypto", venue.lower(), symbol
    if kind == "DEX":
        return "dex", venue.lower(), symbol
    raise UnknownInstrumentError(f"unknown instrument kind in {instrument_id!r}")


def hive_symbol(instrument: Instrument) -> str:
    """Path-safe symbol for Hive partitions (slash and colon become hyphen)."""
    return instrument.symbol.replace("/", "-").replace(":", "-")


def looks_like_address(value: str) -> bool:
    """Return True if ``value`` looks like an EVM or Solana address."""
    return bool(_EVM.match(value) or (_SOL.match(value) and not value.isalpha()))


def looks_like_crypto_symbol(value: str) -> bool:
    """Return True for CCXT unified symbols (``BTC/USDT`` or perp ``BTC/USDT:USDT``)."""
    return "/" in value and not value.startswith("http")


class InstrumentCatalog:
    """Instrument registry: ticker → id, aliases, asset class, venue.

    Resolution is local. DEX ticker collisions are recorded as ``alternatives``;
    DexScreener is the adapter that picks the pool, not this module.
    """

    def __init__(self, store: InstrumentStore | None = None) -> None:
        """Create a catalog, optionally backed by Store persistence.

        Args:
            store: InstrumentStore adapter. ``None`` keeps records in memory only.
        """
        self._store = store
        self._by_id: dict[str, Instrument] = {}
        self._by_alias: dict[str, str] = {}

    def register(self, instrument: Instrument) -> Instrument:
        """Persist ``instrument`` and index aliases (last-write-wins per alias)."""
        self._by_id[instrument.instrument_id] = instrument
        self._index_alias(instrument.instrument_id, instrument.instrument_id)
        self._index_alias(instrument.symbol, instrument.instrument_id)
        for alias in instrument.aliases:
            self._index_alias(alias, instrument.instrument_id)
        if self._store is not None:
            self._store.upsert_instrument(instrument)
        return instrument

    def get(self, instrument_id: str) -> Instrument | None:
        """Return a cached or stored Instrument."""
        found = self._by_id.get(instrument_id)
        if found is not None:
            return found
        if self._store is not None:
            loaded = self._store.get_instrument(instrument_id)
            if loaded is not None:
                self._by_id[instrument_id] = loaded
                return loaded
        return None

    def resolve(
        self,
        query: str,
        *,
        asset_class: AssetClass | None = None,
        venue: str | None = None,
    ) -> Instrument:
        """Resolve ``query`` to an Instrument, synthesizing a new one if needed.

        Args:
            query: Stable id, ticker, ``venue:symbol``, ``chain:address``, or Dex URL.
            asset_class: Hint when the query is ambiguous (``PEPE`` vs equity).
            venue: MIC / CCXT id / chain override.

        Returns:
            Existing or newly registered Instrument.

        Raises:
            UnknownInstrumentError: Empty query or malformed id.
        """
        text = query.strip()
        if not text:
            raise UnknownInstrumentError("empty instrument query")

        by_id = self.get(text)
        if by_id is not None:
            return by_id
        alias = self._lookup_alias(text)
        if alias is not None:
            return alias

        built = self._synthesize(text, asset_class=asset_class, venue=venue)
        return self.register(built)

    def add_alias(self, instrument_id: str, alias: str) -> None:
        """Attach ``alias`` to an existing Instrument (ticker reuse / rename)."""
        inst = self.get(instrument_id)
        if inst is None:
            raise UnknownInstrumentError(instrument_id)
        aliases = tuple(dict.fromkeys([*inst.aliases, alias]))
        updated = inst.model_copy(update={"aliases": aliases})
        self.register(updated)

    def with_alternatives(self, instrument: Instrument, alternatives: tuple[str, ...]) -> Instrument:
        """Return ``instrument`` with DEX ticker-collision alternatives recorded."""
        updated = instrument.model_copy(update={"alternatives": alternatives})
        return self.register(updated)

    def _lookup_alias(self, raw: str) -> Instrument | None:
        key = _alias_key(raw)
        instrument_id = self._by_alias.get(key)
        if instrument_id:
            return self.get(instrument_id)
        if self._store is not None:
            found = self._store.find_by_alias(raw)
            if found is not None:
                self.register(found)
                return found
        return None

    def _index_alias(self, alias: str, instrument_id: str) -> None:
        self._by_alias[_alias_key(alias)] = instrument_id

    def _synthesize(
        self,
        text: str,
        *,
        asset_class: AssetClass | None,
        venue: str | None,
    ) -> Instrument:
        if _ID.match(text):
            cls, ven, sym = parse_instrument_id(text)
            return _build(cls, ven, sym, aliases=(text, sym))

        url = _PAIR_URL.match(text)
        if url:
            chain, pair = url.group(1).lower(), url.group(2)
            iid = f"DEX:{chain}:{pair}"
            return _build("dex", chain, pair, aliases=(text, pair, iid))

        inferred = asset_class or _infer_asset_class(text)
        if inferred == "dex":
            chain, rest = _split_chain(text, venue)
            iid = f"DEX:{chain}:{rest}"
            return _build("dex", chain, rest, aliases=(text, rest, iid))
        if inferred == "crypto":
            exch, sym = _split_crypto(text, venue)
            iid = f"CRYPTO:{exch.upper()}:{sym}"
            return _build("crypto", exch, sym, aliases=(text, sym, iid))

        mic = (venue or _equity_venue(text)).upper()
        symbol = text.split(":")[-1].upper()
        iid = f"EQ:{mic}:{symbol}"
        return _build("equity", mic, symbol, aliases=(text, symbol, iid))


def instruments_from_row(row: Mapping[str, object]) -> Instrument:
    """Rehydrate an Instrument from a DuckDB / mapping row."""
    aliases_raw = row.get("aliases") or "[]"
    alts_raw = row.get("alternatives") or "[]"
    if not isinstance(aliases_raw, str):
        aliases_raw = json.dumps(aliases_raw)
    if not isinstance(alts_raw, str):
        alts_raw = json.dumps(alts_raw)
    pair_created = row.get("pair_created_at")
    created = ensure_utc(pair_created) if isinstance(pair_created, datetime) else None
    asset_class = str(row["asset_class"])
    if asset_class not in {"equity", "crypto", "dex"}:
        raise UnknownInstrumentError(f"bad asset_class {asset_class!r}")
    return Instrument(
        instrument_id=str(row["instrument_id"]),
        asset_class=asset_class,  # type: ignore[arg-type]
        venue=str(row["venue"]),
        symbol=str(row["symbol"]),
        session_tz=str(row["session_tz"]),
        aliases=tuple(json.loads(aliases_raw)),
        alternatives=tuple(json.loads(alts_raw)),
        pair_created_at=created,
        base_token=str(row["base_token"]) if row.get("base_token") else None,
        quote_token=str(row["quote_token"]) if row.get("quote_token") else None,
    )


def _build(asset_class: AssetClass, venue: str, symbol: str, aliases: tuple[str, ...]) -> Instrument:
    session_tz = "America/New_York" if asset_class == "equity" else "UTC"
    if asset_class == "equity":
        iid = f"EQ:{venue.upper()}:{symbol.upper()}"
        venue_out = venue.upper()
        symbol_out = symbol.upper()
    elif asset_class == "crypto":
        iid = f"CRYPTO:{venue.upper()}:{symbol}"
        venue_out = venue.lower()
        symbol_out = symbol
    else:
        iid = f"DEX:{venue.lower()}:{symbol}"
        venue_out = venue.lower()
        symbol_out = symbol
    return Instrument(
        instrument_id=iid,
        asset_class=asset_class,
        venue=venue_out,
        symbol=symbol_out,
        session_tz=session_tz,
        aliases=aliases,
    )


def _infer_asset_class(text: str) -> AssetClass:
    if text.startswith("http") or looks_like_address(text) or _PAIR_URL.match(text):
        return "dex"
    if ":" in text and not text.startswith("http"):
        head, _, rest = text.partition(":")
        if rest and looks_like_address(rest):
            return "dex"
        if looks_like_crypto_symbol(rest) or looks_like_crypto_symbol(text):
            return "crypto"
    if looks_like_crypto_symbol(text):
        return "crypto"
    return "equity"


def _split_chain(text: str, venue: str | None) -> tuple[str, str]:
    if venue:
        return venue.lower(), text.split(":")[-1]
    if ":" in text:
        chain, _, rest = text.partition(":")
        return chain.lower(), rest
    return "solana", text


def _split_crypto(text: str, venue: str | None) -> tuple[str, str]:
    if venue:
        return venue.lower(), text.split(":", 1)[-1] if text.lower().startswith(venue.lower() + ":") else text
    if ":" in text and "/" in text:
        head, _, rest = text.partition(":")
        if "/" in rest:
            return head.lower(), rest
        # perp: BTC/USDT:USDT — whole thing is the symbol
        return "binance", text
    return "binance", text


def _equity_venue(text: str) -> str:
    symbol = text.split(":")[-1].upper()
    return "XNYS" if symbol in _NYSE_TICKERS else "XNAS"


def _alias_key(alias: str) -> str:
    return alias.strip().lower()


def dex_url_parts(url: str) -> tuple[str, str] | None:
    """Return ``(chain, pair)`` from a DexScreener pair URL, else None."""
    match = _PAIR_URL.match(url.strip())
    if match is None:
        return None
    return match.group(1).lower(), match.group(2)
