"""Shared fixtures and helpers. Tests never hit live vendor APIs."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from backtester.catalog.instruments import InstrumentCatalog
from backtester.domain import Instrument, VendorBar
from backtester.store.parquet_duckdb import ParquetDuckDBStore
from backtester.timeframes import Timeframe

FIXTURES = Path(__file__).parent / "fixtures"


def load_json(name: str) -> object:
    """Load a vendor fixture file."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def tmp_store(tmp_path: Path) -> Iterator[ParquetDuckDBStore]:
    store = ParquetDuckDBStore(tmp_path / "data")
    yield store
    store.close()


@pytest.fixture
def catalog() -> InstrumentCatalog:
    return InstrumentCatalog()


def aapl() -> Instrument:
    return Instrument(
        instrument_id="EQ:XNAS:AAPL",
        asset_class="equity",
        venue="XNAS",
        symbol="AAPL",
        session_tz="America/New_York",
        aliases=("AAPL",),
    )


def btc() -> Instrument:
    return Instrument(
        instrument_id="CRYPTO:BINANCE:BTC/USDT",
        asset_class="crypto",
        venue="binance",
        symbol="BTC/USDT",
        session_tz="UTC",
        aliases=("BTC/USDT",),
    )


def pepe_pool() -> Instrument:
    return Instrument(
        instrument_id="DEX:solana:PepeHighLiq11111111111111111111111111111",
        asset_class="dex",
        venue="solana",
        symbol="PepeHighLiq11111111111111111111111111111",
        session_tz="UTC",
        aliases=("PEPE",),
        pair_created_at=datetime.fromtimestamp(1700000000, tz=UTC),
    )


def utc(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


def vendor_bar(
    ts: datetime,
    *,
    source: str = "yahoo",
    timeframe: Timeframe = "1d",
    o: float = 10.0,
    h: float = 11.0,
    low: float = 9.0,
    c: float = 10.5,
    v: float = 100.0,
    ts_kind: str = "utc",
) -> VendorBar:
    return VendorBar(
        source=source,
        vendor_symbol="AAPL",
        timeframe=timeframe,
        ts=ts,
        open=o,
        high=h,
        low=low,
        close=c,
        volume=v,
        ts_kind=ts_kind,  # type: ignore[arg-type]
    )


def bars_frame(instrument: Instrument, stamps: list[datetime], *, source: str, timeframe: Timeframe) -> pl.DataFrame:
    rows = []
    for ts in stamps:
        rows.append(
            {
                "instrument_id": instrument.instrument_id,
                "source": source,
                "timeframe": timeframe,
                "ts_utc": ts,
                "open": 10.0,
                "high": 11.0,
                "low": 9.0,
                "close": 10.5,
                "volume": 100.0,
                "is_partial": False,
                "quality": "ok",
            }
        )
    return pl.DataFrame(rows).with_columns(pl.col("ts_utc").dt.convert_time_zone("UTC"))


def hour_range(start: datetime, hours: int) -> list[datetime]:
    return [start + timedelta(hours=i) for i in range(hours)]
