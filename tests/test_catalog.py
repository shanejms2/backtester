"""Catalog: stable ids, aliases, ticker collisions recorded not mixed."""

from __future__ import annotations

from backtester.catalog.instruments import InstrumentCatalog, hive_symbol, parse_instrument_id, yahoo_ticker
from backtester.domain import Instrument
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


def test_irx_caret_alias() -> None:
    cat = InstrumentCatalog()
    by_caret = cat.resolve("^IRX")
    assert by_caret.instrument_id == "EQ:XNAS:IRX"
    assert by_caret.symbol == "IRX"
    assert "^IRX" in by_caret.aliases
    assert cat.resolve("IRX").instrument_id == by_caret.instrument_id
    assert cat.resolve("EQ:XNAS:IRX").instrument_id == by_caret.instrument_id
    fresh = InstrumentCatalog()
    by_id = fresh.resolve("EQ:XNAS:IRX")
    assert "^IRX" in by_id.aliases
    assert fresh.resolve("^IRX").instrument_id == by_id.instrument_id


def test_ndx_caret_alias() -> None:
    cat = InstrumentCatalog()
    ndx = cat.resolve("^NDX")
    assert ndx.instrument_id == "EQ:XNAS:NDX"
    assert ndx.symbol == "NDX"
    assert yahoo_ticker(ndx) == "^NDX"


def test_yahoo_ticker_caret_for_irx() -> None:
    cat = InstrumentCatalog()
    assert yahoo_ticker(cat.resolve("AAPL")) == "AAPL"
    irx = Instrument(
        instrument_id="EQ:XNAS:IRX",
        asset_class="equity",
        venue="XNAS",
        symbol="IRX",
        session_tz="America/New_York",
    )
    assert yahoo_ticker(irx) == "^IRX"


def test_nse_niftybees_yahoo_suffix() -> None:
    cat = InstrumentCatalog()
    bees = cat.resolve("NIFTYBEES.NS")
    assert bees.instrument_id == "EQ:XNSE:NIFTYBEES"
    assert bees.venue == "XNSE"
    assert bees.session_tz == "Asia/Kolkata"
    assert yahoo_ticker(bees) == "NIFTYBEES.NS"
    assert cat.resolve("EQ:XNSE:NIFTYBEES").instrument_id == bees.instrument_id


def test_fx_inr_yahoo_pair() -> None:
    cat = InstrumentCatalog()
    inr = cat.resolve("INR=X")
    assert inr.instrument_id == "EQ:CCY:INR"
    assert inr.venue == "CCY"
    assert yahoo_ticker(inr) == "INR=X"
    assert cat.resolve("EQ:CCY:INR").instrument_id == inr.instrument_id


def test_arcx_venue_override() -> None:
    cat = InstrumentCatalog()
    spy = cat.resolve("SPY", venue="ARCX")
    assert spy.instrument_id == "EQ:ARCX:SPY"
    assert spy.venue == "ARCX"


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
