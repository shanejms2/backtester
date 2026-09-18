"""Catalog: stable ids, aliases, ticker collisions recorded not mixed."""

from __future__ import annotations

from backtester.catalog.instruments import InstrumentCatalog, hive_symbol, parse_instrument_id
from tests.conftest import pepe_pool


def test_equity_ticker_to_id() -> None:
    cat = InstrumentCatalog()
    inst = cat.resolve("AAPL")
    assert inst.instrument_id == "EQ:XNAS:AAPL"
    assert inst.session_tz == "America/New_York"
    again = cat.resolve("aapl")
    assert again.instrument_id == inst.instrument_id


def test_crypto_symbol() -> None:
    cat = InstrumentCatalog()
    inst = cat.resolve("BTC/USDT", asset_class="crypto")
    assert inst.instrument_id == "CRYPTO:BINANCE:BTC/USDT"
    assert inst.session_tz == "UTC"


def test_perp_colon_in_symbol() -> None:
    cls, venue, symbol = parse_instrument_id("CRYPTO:BINANCE:BTC/USDT:USDT")
    assert cls == "crypto"
    assert venue == "binance"
    assert symbol == "BTC/USDT:USDT"
    assert hive_symbol(InstrumentCatalog().resolve("CRYPTO:BINANCE:BTC/USDT:USDT")) == "BTC-USDT-USDT"


def test_dex_url() -> None:
    cat = InstrumentCatalog()
    inst = cat.resolve("https://dexscreener.com/solana/abc123", asset_class="dex")
    assert inst.instrument_id == "DEX:solana:abc123"


def test_ticker_reuse_alias() -> None:
    cat = InstrumentCatalog()
    inst = cat.resolve("FB")
    cat.add_alias(inst.instrument_id, "META")
    assert cat.resolve("META").instrument_id == inst.instrument_id


def test_alternatives_not_mixed() -> None:
    cat = InstrumentCatalog()
    pepe = pepe_pool()
    cat.register(pepe)
    updated = cat.with_alternatives(pepe, ("DEX:ethereum:0xother",))
    assert updated.instrument_id == pepe.instrument_id
    assert "DEX:ethereum:0xother" in updated.alternatives
