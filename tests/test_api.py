"""HTTP API tests. No live vendors; optional in-memory store."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import polars as pl
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from backtester.api.app import create_app
from backtester.domain import CoverageInterval, Instrument, empty_bars
from backtester.settings import Settings
from backtester.timeframes import Timeframe


class _MemoryStore:
    def __init__(self) -> None:
        self.instruments: dict[str, Instrument] = {}
        self.aliases: dict[str, str] = {}
        self.cov: list[CoverageInterval] = []
        self.bars = empty_bars()

    def write_batch(self, bars: pl.DataFrame) -> Any:
        raise NotImplementedError

    def scan(
        self,
        instrument_id: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        *,
        columns: list[str] | None = None,
        source: str | None = None,
    ) -> pl.LazyFrame:
        frame = self.bars
        if frame.is_empty():
            return empty_bars().lazy()
        lf = frame.lazy().filter(
            (pl.col("instrument_id") == instrument_id)
            & (pl.col("timeframe") == timeframe)
            & (pl.col("ts_utc") >= start)
            & (pl.col("ts_utc") <= end)
        )
        if source is not None:
            lf = lf.filter(pl.col("source") == source)
        return lf

    def coverage(
        self,
        instrument_id: str,
        timeframe: Timeframe,
        source: str,
    ) -> list[CoverageInterval]:
        return [
            c
            for c in self.cov
            if c.instrument_id == instrument_id and c.timeframe == timeframe and c.source == source
        ]

    def list_coverage(self) -> list[CoverageInterval]:
        return list(self.cov)

    def update_coverage(
        self,
        instrument_id: str,
        timeframe: Timeframe,
        source: str,
        start: datetime,
        end: datetime,
    ) -> None:
        raise NotImplementedError

    def drop_coverage(
        self,
        instrument_id: str,
        timeframe: Timeframe,
        source: str,
        start: datetime,
        end: datetime,
    ) -> None:
        raise NotImplementedError

    def compact(self, instrument_id: str | None = None) -> int:
        return 0

    def prune_hot_1m(self, *, hot_days: int, now: datetime | None = None) -> int:
        return 0

    def write_snapshots(self, snapshots: pl.DataFrame) -> Any:
        raise NotImplementedError

    def last_snapshot_poll(self, instrument_id: str, source: str) -> datetime | None:
        return None

    def upsert_instrument(self, instrument: Instrument) -> None:
        self.instruments[instrument.instrument_id] = instrument
        for alias in instrument.aliases:
            self.aliases[alias.lower()] = instrument.instrument_id

    def get_instrument(self, instrument_id: str) -> Instrument | None:
        return self.instruments.get(instrument_id)

    def find_by_alias(self, alias: str) -> Instrument | None:
        iid = self.aliases.get(alias.lower())
        return self.instruments.get(iid) if iid else None


@pytest.fixture
def api_settings() -> Settings:
    return Settings(
        api_key=SecretStr("test-key"),
        display_tz="UTC",
        api_max_bars=5,
        clickhouse_password=SecretStr(""),
    )


@pytest.fixture
def store() -> _MemoryStore:
    mem = _MemoryStore()
    inst = Instrument(
        instrument_id="EQ:XNAS:AAPL",
        asset_class="equity",
        venue="XNAS",
        symbol="AAPL",
        session_tz="America/New_York",
        aliases=("AAPL",),
    )
    mem.upsert_instrument(inst)
    mem.cov.append(
        CoverageInterval(
            instrument_id="EQ:XNAS:AAPL",
            timeframe="1d",
            source="yahoo",
            start_utc=datetime(2024, 1, 2, tzinfo=UTC),
            end_utc=datetime(2024, 1, 4, tzinfo=UTC),
        )
    )
    mem.bars = pl.DataFrame(
        {
            "instrument_id": ["EQ:XNAS:AAPL"] * 3,
            "source": ["yahoo"] * 3,
            "timeframe": ["1d"] * 3,
            "ts_utc": [
                datetime(2024, 1, 2, 14, 30, tzinfo=UTC),
                datetime(2024, 1, 3, 14, 30, tzinfo=UTC),
                datetime(2024, 1, 4, 14, 30, tzinfo=UTC),
            ],
            "open": [1.0, 2.0, 3.0],
            "high": [1.5, 2.5, 3.5],
            "low": [0.5, 1.5, 2.5],
            "close": [1.2, 2.2, 3.2],
            "volume": [100.0, 200.0, 300.0],
            "is_partial": [False, False, False],
            "quality": ["ok", "ok", "ok"],
            "adj_close": [1.2, 2.2, 3.2],
        }
    )
    return mem


@pytest.fixture
def client(api_settings: Settings, store: _MemoryStore):
    app = create_app(settings=api_settings, store=store)  # type: ignore[arg-type]
    with TestClient(app) as test_client:
        yield test_client


def test_health_open(client: TestClient) -> None:
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_v1_requires_api_key(client: TestClient) -> None:
    res = client.get("/v1/instruments", params={"q": "AAPL"})
    assert res.status_code == 401


def test_instruments_and_bars(client: TestClient) -> None:
    headers = {"X-API-Key": "test-key"}
    inst = client.get("/v1/instruments", params={"q": "AAPL"}, headers=headers)
    assert inst.status_code == 200
    assert inst.json()["instrument_id"] == "EQ:XNAS:AAPL"

    cov = client.get(
        "/v1/coverage",
        params={"instrument_id": "EQ:XNAS:AAPL", "tf": "1d"},
        headers=headers,
    )
    assert cov.status_code == 200
    assert len(cov.json()["coverage"]) == 1

    bars = client.get(
        "/v1/bars",
        params={
            "instrument_id": "EQ:XNAS:AAPL",
            "tf": "1d",
            "from": "2024-01-01",
            "to": "2024-01-05",
            "source": "yahoo",
            "tz": "UTC",
        },
        headers=headers,
    )
    assert bars.status_code == 200
    body = bars.json()
    assert body["count"] == 3
    assert body["truncated"] is False
    assert body["bars"][0]["close"] == 1.2
    assert "ts_utc" in body["bars"][0]


def test_bars_respect_max_limit(client: TestClient) -> None:
    headers = {"X-API-Key": "test-key"}
    bars = client.get(
        "/v1/bars",
        params={
            "instrument_id": "EQ:XNAS:AAPL",
            "tf": "1d",
            "from": "2024-01-01",
            "to": "2024-01-05",
            "limit": 2,
        },
        headers=headers,
    )
    assert bars.status_code == 200
    body = bars.json()
    assert body["count"] == 2
    assert body["truncated"] is True
