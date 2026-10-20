"""Normalize: ET→UTC, OHLC reject, partial last bar, no snapshot OHLC."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from backtester.domain import PairSnapshot, VendorBar
from backtester.normalize.bars import (
    is_partial_bar,
    normalize_vendor_bars,
    ohlc_valid,
    repair_price_level_glitches,
    snapshot_quality,
    snapshots_to_frame,
    vendor_ts_to_utc,
)
from tests.conftest import aapl, vendor_bar


def test_ohlc_reject() -> None:
    assert ohlc_valid(10, 12, 9, 11)
    assert not ohlc_valid(10, 11, 9.5, 12)  # high < close
    assert not ohlc_valid(10, 12, 10.5, 11)  # low > open
    assert not ohlc_valid(float("nan"), 12, 9, 11)


def test_normalize_drops_invalid() -> None:
    inst = aapl()
    now = datetime(2025, 6, 1, tzinfo=UTC)
    good = vendor_bar(datetime(2025, 1, 2, 14, 30, tzinfo=UTC), timeframe="1d")
    bad = vendor_bar(
        datetime(2025, 1, 3, 14, 30, tzinfo=UTC),
        timeframe="1d",
        o=10,
        h=9,
        low=8,
        c=9.5,
    )
    future = vendor_bar(datetime(2026, 1, 1, tzinfo=UTC), timeframe="1d")
    frame = normalize_vendor_bars([good, bad, future], inst, "1d", now=now)
    assert frame.height == 1
    assert frame["ts_utc"][0].date().isoformat() == "2025-01-02"


def test_av_naive_eastern_to_utc() -> None:
    inst = aapl()
    naive = datetime(2025, 3, 10, 9, 30)  # EDT date
    bar = VendorBar(
        source="alphavantage",
        vendor_symbol="AAPL",
        timeframe="1h",
        ts=naive,
        open=1,
        high=1,
        low=1,
        close=1,
        volume=1,
        ts_kind="naive_session",
    )
    utc_ts = vendor_ts_to_utc(bar, inst)
    assert utc_ts.tzinfo is not None
    assert utc_ts.utcoffset().total_seconds() == 0
    ny = utc_ts.astimezone(ZoneInfo("America/New_York"))
    assert ny.hour == 9 and ny.minute == 30
    # 2025-03-10 is after spring-forward → UTC-4 → 13:30 UTC
    assert utc_ts.hour == 13


def test_av_daily_session_date_is_rth_open() -> None:
    inst = aapl()
    bar = VendorBar(
        source="alphavantage",
        vendor_symbol="AAPL",
        timeframe="1d",
        ts=datetime(2025, 1, 2),
        open=1,
        high=1,
        low=1,
        close=1,
        volume=1,
        ts_kind="session_date",
    )
    utc_ts = vendor_ts_to_utc(bar, inst)
    ny = utc_ts.astimezone(ZoneInfo("America/New_York"))
    assert ny.hour == 9 and ny.minute == 30
    assert ny.date().isoformat() == "2025-01-02"


def test_partial_last_bar() -> None:
    now = datetime(2025, 6, 1, 10, 30, tzinfo=UTC)
    open_ts = datetime(2025, 6, 1, 10, 0, tzinfo=UTC)
    assert is_partial_bar(open_ts, "1h", now)
    assert not is_partial_bar(datetime(2025, 6, 1, 9, 0, tzinfo=UTC), "1h", now)


def test_dedup_last_write_wins() -> None:
    inst = aapl()
    ts = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)
    first = vendor_bar(ts, c=10.0)
    second = vendor_bar(ts, c=11.0)
    frame = normalize_vendor_bars([first, second], inst, "1d", now=datetime(2025, 6, 1, tzinfo=UTC))
    assert frame.height == 1
    assert frame["close"][0] == pytest.approx(11.0)


def test_adj_close_null_falls_back_to_close() -> None:
    inst = aapl()
    ts = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)
    frame = normalize_vendor_bars([vendor_bar(ts, c=10.5)], inst, "1d", now=datetime(2025, 6, 1, tzinfo=UTC))
    assert float(frame["adj_close"][0]) == pytest.approx(10.5)


def test_adj_close_from_vendor_is_stored() -> None:
    inst = aapl()
    ts = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)
    bar = VendorBar(
        source="yahoo",
        vendor_symbol="AAPL",
        timeframe="1d",
        ts=ts,
        open=10,
        high=11,
        low=9,
        close=10.5,
        volume=100,
        adj_close=8.0,
    )
    frame = normalize_vendor_bars([bar], inst, "1d", now=datetime(2025, 6, 1, tzinfo=UTC))
    assert float(frame["close"][0]) == pytest.approx(10.5)
    assert float(frame["adj_close"][0]) == pytest.approx(8.0)


def _level_frame(closes: list[float]) -> pl.DataFrame:
    stamps = [datetime(2019, 12, 1 + i, 18, 30, tzinfo=UTC) for i in range(len(closes))]
    return pl.DataFrame(
        {
            "ts_utc": stamps,
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": [300_000.0] * len(closes),
            "adj_close": closes,
        }
    ).with_columns(pl.col("ts_utc").dt.convert_time_zone("UTC"))


def test_repair_scales_tenth_price_island() -> None:
    closes = [130.0] * 8 + [13.0, 13.1] + [129.0] * 8
    out = repair_price_level_glitches(_level_frame(closes))
    assert float(out["close"][8]) == pytest.approx(130.0)
    assert float(out["close"][9]) == pytest.approx(131.0)
    assert float(out["open"][8]) == pytest.approx(130.0)
    assert float(out["adj_close"][8]) == pytest.approx(130.0)
    assert float(out["volume"][8]) == pytest.approx(30_000.0)
    assert float(out["close"][0]) == pytest.approx(130.0)
    assert float(out["close"][10]) == pytest.approx(129.0)


def test_repair_scales_tenx_price_island() -> None:
    closes = [130.0] * 8 + [1300.0, 1310.0] + [129.0] * 8
    out = repair_price_level_glitches(_level_frame(closes))
    assert float(out["close"][8]) == pytest.approx(130.0)
    assert float(out["close"][9]) == pytest.approx(131.0)
    assert float(out["volume"][8]) == pytest.approx(3_000_000.0)


def test_repair_leaves_splits_and_crashes() -> None:
    split = [100.0] * 10 + [10.0] * 10
    crash = [100.0] * 10 + [70.0] * 10
    split_out = repair_price_level_glitches(_level_frame(split))
    crash_out = repair_price_level_glitches(_level_frame(crash))
    assert split_out["close"].to_list() == split
    assert crash_out["close"].to_list() == crash


def test_normalize_repairs_yahoo_decimal_island() -> None:
    inst = aapl()
    now = datetime(2025, 6, 1, tzinfo=UTC)
    bars = []
    closes = [130.0] * 8 + [13.0, 13.1] + [129.0] * 8
    for i, close in enumerate(closes):
        bars.append(
            vendor_bar(
                datetime(2019, 12, 1 + i, 18, 30, tzinfo=UTC),
                o=close,
                h=close * 1.01,
                low=close * 0.99,
                c=close,
                v=300_000.0,
            )
        )
    frame = normalize_vendor_bars(bars, inst, "1d", now=now)
    assert float(frame["close"][8]) == pytest.approx(130.0)
    assert float(frame["close"][9]) == pytest.approx(131.0)


def test_snapshot_quality_suspect_without_price() -> None:
    assert snapshot_quality(None, 1000) == "suspect"
    assert snapshot_quality(1.0, 0) == "suspect"
    assert snapshot_quality(1.0, 5000) == "ok"


def test_snapshots_are_not_bars() -> None:
    snap = PairSnapshot(
        chain_id="solana",
        dex_id="raydium",
        pair_address="abc",
        base_symbol="PEPE",
        quote_symbol="USDC",
        price_usd=0.00001,
        liquidity_usd=5000,
        volume_h24=1000,
        price_change_h24=12.5,
        polled_at_utc=datetime(2025, 1, 1, tzinfo=UTC),
    )
    frame = snapshots_to_frame([snap])
    assert "open" not in frame.columns
    assert "price_change_h24" in frame.columns
