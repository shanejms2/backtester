"""Coverage-first ingest: gaps, idempotency, pairCreatedAt clip, snapshots."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from backtester.adapters.base import SourceAdapter
from backtester.calendar.session import SessionCalendar
from backtester.catalog.instruments import InstrumentCatalog
from backtester.domain import IngestRequest, Instrument, PairSnapshot, VendorBar
from backtester.ingest.engine import IngestEngine
from backtester.settings import Settings
from backtester.store.parquet_duckdb import ParquetDuckDBStore
from backtester.timeframes import Timeframe
from tests.conftest import aapl, btc, pepe_pool, utc, vendor_bar


class FakeAdapter(SourceAdapter):
    """In-memory SourceAdapter. Counts fetches for idempotency tests."""

    def __init__(self, source_id: str, bars: list[VendorBar] | None = None) -> None:
        self.source_id = source_id
        self.bars = bars or []
        self.calls = 0
        self.ranges: list[tuple[datetime, datetime]] = []

    def fetch_pages(
        self,
        instrument: Instrument,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        *,
        extended_hours: bool = False,
    ) -> Iterator[list[VendorBar]]:
        del instrument, timeframe, extended_hours
        self.calls += 1
        self.ranges.append((start, end))
        page = [b for b in self.bars if start <= b.ts < end]
        if page:
            yield page

    def truncated(self) -> bool:
        return False


class FakeDex(SourceAdapter):
    source_id = "dexscreener"

    def __init__(self, inst: Instrument) -> None:
        self._inst = inst

    def fetch_pages(self, *args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        return
        yield

    def resolve(self, query: str) -> Instrument:
        del query
        return self._inst

    def fetch_snapshots(self, instrument: Instrument) -> list[PairSnapshot]:
        del instrument
        return [
            PairSnapshot(
                chain_id="solana",
                dex_id="raydium",
                pair_address=self._inst.symbol,
                base_symbol="PEPE",
                quote_symbol="USDC",
                price_usd=0.00001,
                liquidity_usd=1_000_000,
                volume_h24=5000,
                price_change_h24=8.0,
                pair_created_at=self._inst.pair_created_at,
                polled_at_utc=datetime(2024, 6, 1, tzinfo=UTC),
            )
        ]


def _engine(
    store: ParquetDuckDBStore,
    adapters: dict[str, SourceAdapter],
    *,
    dex: FakeDex | None = None,
) -> IngestEngine:
    cat = InstrumentCatalog(store)
    return IngestEngine(
        store,
        catalog=cat,
        calendar=SessionCalendar(),
        settings=Settings(data_dir=store.root),
        adapters=adapters,
        dex_resolver=dex,
        now=lambda: datetime(2025, 6, 1, tzinfo=UTC),
    )


def test_idempotent_second_request_is_noop(tmp_store: ParquetDuckDBStore) -> None:
    inst = aapl()
    tmp_store.upsert_instrument(inst)
    ts = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)
    fake = FakeAdapter("yahoo", [vendor_bar(ts, timeframe="1d")])
    engine = _engine(tmp_store, {"yahoo": fake})
    req = IngestRequest(
        query="AAPL",
        timeframe="1d",
        start=utc(2025, 1, 2),
        end=utc(2025, 1, 3),
        source="yahoo",
        tz="UTC",
    )
    first = engine.ingest(req)
    assert first.rows_written >= 1
    assert fake.calls == 1
    second = engine.ingest(req)
    assert second.noop
    assert fake.calls == 1


def test_equity_weekend_not_fetched(tmp_store: ParquetDuckDBStore) -> None:
    inst = aapl()
    tmp_store.upsert_instrument(inst)
    fake = FakeAdapter("yahoo", [])
    engine = _engine(tmp_store, {"yahoo": fake})
    ny = ZoneInfo("America/New_York")
    engine.ingest(
        IngestRequest(
            query="AAPL",
            timeframe="1d",
            start=datetime(2025, 1, 4, tzinfo=ny),  # Saturday NY
            end=datetime(2025, 1, 6, tzinfo=ny),  # Monday NY midnight (exclusive)
            source="yahoo",
            tz="UTC",
        )
    )
    assert fake.calls == 0


def test_crypto_weekend_fetched(tmp_store: ParquetDuckDBStore) -> None:
    inst = btc()
    tmp_store.upsert_instrument(inst)
    ts = utc(2025, 1, 4, 12)
    fake = FakeAdapter("binance", [vendor_bar(ts, source="binance", timeframe="1h")])
    engine = _engine(tmp_store, {"binance": fake})
    result = engine.ingest(
        IngestRequest(
            query="BTC/USDT",
            timeframe="1h",
            start=utc(2025, 1, 4),
            end=utc(2025, 1, 4, 13),
            source="binance",
            asset_class="crypto",
            tz="UTC",
        )
    )
    assert fake.calls == 1
    assert result.rows_written == 1


def test_pair_created_at_clips_coverage(tmp_store: ParquetDuckDBStore) -> None:
    inst = pepe_pool()
    tmp_store.upsert_instrument(inst)
    created = inst.pair_created_at
    assert created is not None
    bars = [
        vendor_bar(
            created + timedelta(hours=1),
            source="geckoterminal",
            timeframe="1h",
            ts_kind="utc",
        )
    ]
    fake = FakeAdapter("geckoterminal", bars)
    engine = _engine(tmp_store, {"geckoterminal": fake})
    engine.ingest(
        IngestRequest(
            query=inst.instrument_id,
            timeframe="1h",
            start=utc(2010, 1, 1),
            end=created + timedelta(hours=2),
            source="geckoterminal",
            asset_class="dex",
            tz="UTC",
        )
    )
    assert fake.calls == 1
    assert fake.ranges[0][0] >= created


def test_snapshots_do_not_write_bars(tmp_store: ParquetDuckDBStore) -> None:
    inst = pepe_pool()
    tmp_store.upsert_instrument(inst)
    dex = FakeDex(inst)
    engine = _engine(tmp_store, {"dexscreener": dex}, dex=dex)
    result = engine.ingest(
        IngestRequest(
            query=inst.instrument_id,
            timeframe="1h",
            start=utc(2024, 1, 1),
            end=utc(2024, 1, 2),
            source="dexscreener",
            asset_class="dex",
            tz="UTC",
        )
    )
    assert result.rows_written == 1
    bars = tmp_store.scan(inst.instrument_id, "1h", utc(2024, 1, 1), utc(2024, 1, 3)).collect()
    assert bars.height == 0


def test_partial_flag_on_live_last_bar(tmp_store: ParquetDuckDBStore) -> None:
    inst = btc()
    tmp_store.upsert_instrument(inst)
    now = datetime(2025, 6, 1, 10, 30, tzinfo=UTC)
    open_ts = datetime(2025, 6, 1, 10, 0, tzinfo=UTC)
    fake = FakeAdapter("binance", [vendor_bar(open_ts, source="binance", timeframe="1h")])
    engine = IngestEngine(
        tmp_store,
        catalog=InstrumentCatalog(tmp_store),
        adapters={"binance": fake},
        settings=Settings(data_dir=tmp_store.root),
        now=lambda: now,
    )
    engine.ingest(
        IngestRequest(
            query="BTC/USDT",
            timeframe="1h",
            start=open_ts,
            end=open_ts + timedelta(hours=1),
            source="binance",
            asset_class="crypto",
            tz="UTC",
        )
    )
    out = tmp_store.scan(inst.instrument_id, "1h", open_ts, open_ts + timedelta(hours=1)).collect()
    assert out.height == 1
    assert bool(out["is_partial"][0]) is True
