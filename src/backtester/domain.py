"""Domain types: Instrument, Bar, VendorBar, PairSnapshot, Request, Coverage."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backtester.timeframes import Timeframe

AssetClass = Literal["equity", "crypto", "dex"]
Quality = Literal["ok", "suspect"]
SessionType = Literal["rth", "eth"]
SourceName = str


class Instrument(BaseModel):
    """Stable internal identity for a tradable series.

    Attributes:
        instrument_id: ``EQ:XNAS:AAPL``, ``CRYPTO:BINANCE:BTC/USDT``, or
            ``DEX:solana:<pairAddress>``.
        asset_class: equity, crypto (CEX/perp), or dex.
        venue: MIC (``XNAS``), CCXT id (``binance``), or chain id (``solana``).
        symbol: Ticker, unified CCXT symbol, or pair address (path-safe form is derived).
        session_tz: Venue clock (``America/New_York`` or ``UTC``).
        aliases: Vendor tickers, mints, DexScreener URLs that resolve here.
        alternatives: Other DEX pools for the same ticker (ticker collisions).
        pair_created_at: DEX pool birth (UTC); Coverage cannot start before this.
    """

    model_config = ConfigDict(frozen=True)

    instrument_id: str
    asset_class: AssetClass
    venue: str
    symbol: str
    session_tz: str
    aliases: tuple[str, ...] = ()
    alternatives: tuple[str, ...] = ()
    pair_created_at: datetime | None = None
    base_token: str | None = None
    quote_token: str | None = None


class VendorBar(BaseModel):
    """One OHLCV row as the vendor emitted it, before Normalize."""

    model_config = ConfigDict(frozen=True)

    source: str
    vendor_symbol: str
    timeframe: Timeframe
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_partial: bool | None = None
    ts_kind: Literal["utc", "naive_session", "session_date"] = "utc"


class Bar(BaseModel):
    """Canonical stored Bar. ``ts_local`` is not a column; it is derived on read."""

    model_config = ConfigDict(frozen=True)

    instrument_id: str
    source: str
    timeframe: Timeframe
    ts_utc: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_partial: bool = False
    quality: Quality = "ok"


class PairSnapshot(BaseModel):
    """DexScreener current-state quote. Not a Bar; do not synthesize OHLC."""

    model_config = ConfigDict(frozen=True)

    source: str = "dexscreener"
    chain_id: str
    dex_id: str
    pair_address: str
    base_symbol: str
    quote_symbol: str
    base_address: str | None = None
    quote_address: str | None = None
    price_usd: float | None = None
    price_native: float | None = None
    liquidity_usd: float | None = None
    volume_m5: float | None = None
    volume_h1: float | None = None
    volume_h6: float | None = None
    volume_h24: float | None = None
    price_change_h24: float | None = None
    fdv: float | None = None
    market_cap: float | None = None
    pair_created_at: datetime | None = None
    polled_at_utc: datetime
    quality: Quality = "ok"
    url: str | None = None


class CoverageInterval(BaseModel):
    """Inclusive UTC range already fetched for ``(instrument, timeframe, source)``."""

    model_config = ConfigDict(frozen=True)

    instrument_id: str
    timeframe: Timeframe
    source: str
    start_utc: datetime
    end_utc: datetime


class Interval(BaseModel):
    """Half-open UTC interval ``[start, end)`` used for hole math."""

    model_config = ConfigDict(frozen=True)

    start: datetime
    end: datetime


class IngestRequest(BaseModel):
    """Coverage-first fetch: Instrument X, Timeframe T, from A to B, one Source."""

    query: str
    timeframe: Timeframe
    start: datetime
    end: datetime
    source: str | None = None
    asset_class: AssetClass | None = None
    venue: str | None = None
    tz: str | None = None
    session: SessionType = "rth"
    instrument_id: str | None = None


class WriteResult(BaseModel):
    """Outcome of a Store write (row counts after dedup)."""

    rows_written: int
    partitions: int = 0
    files: tuple[str, ...] = ()


class IngestResult(BaseModel):
    """Outcome of one Request."""

    instrument: Instrument
    source: str
    timeframe: Timeframe
    rows_written: int
    holes_fetched: int
    noop: bool = False
    warnings: tuple[str, ...] = Field(default_factory=tuple)
