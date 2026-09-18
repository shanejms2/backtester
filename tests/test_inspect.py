"""Unexpected Gaps and quality/spike flags. Fixtures only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl

from backtester.calendar.session import SessionCalendar
from backtester.inspect.problems import find_problems
from tests.conftest import aapl, bars_frame, btc, utc


def test_equity_mid_session_hole_is_unexpected() -> None:
    inst = aapl()
    # Tuesday 2024-01-16 RTH 1h: 14:30 UTC (09:30 NY) through 20:30 UTC.
    stamps = [utc(2024, 1, 16, 14, 30) + timedelta(hours=h) for h in range(7)]
    hole = utc(2024, 1, 16, 17, 30)
    stamps = [ts for ts in stamps if ts != hole]
    frame = bars_frame(inst, stamps, source="yahoo", timeframe="1h")
    problems = find_problems(frame, inst, "1h", calendar=SessionCalendar())
    gaps = [p for p in problems if p.kind == "gap"]
    assert len(gaps) == 1
    assert gaps[0].ts_utc == hole


def test_equity_weekend_hole_is_not_unexpected() -> None:
    inst = aapl()
    friday = utc(2024, 1, 19, 14, 30)
    monday = utc(2024, 1, 22, 14, 30)
    frame = bars_frame(inst, [friday, monday], source="yahoo", timeframe="1d")
    problems = find_problems(frame, inst, "1d", calendar=SessionCalendar())
    assert [p for p in problems if p.kind == "gap"] == []


def test_crypto_weekend_hole_is_unexpected() -> None:
    inst = btc()
    friday = utc(2024, 1, 19)
    sunday = utc(2024, 1, 21)
    frame = bars_frame(inst, [friday, sunday], source="binance", timeframe="1d")
    problems = find_problems(frame, inst, "1d", calendar=SessionCalendar())
    gaps = [p for p in problems if p.kind == "gap"]
    assert len(gaps) == 1
    assert gaps[0].ts_utc == utc(2024, 1, 20)


def test_suspect_and_zero_volume_markers() -> None:
    inst = aapl()
    ts = utc(2024, 1, 16, 14, 30)
    frame = bars_frame(inst, [ts], source="yahoo", timeframe="1d")
    frame = frame.with_columns(
        pl.lit("suspect").alias("quality"),
        pl.lit(0.0).alias("volume"),
        pl.lit(True).alias("is_partial"),
    )
    kinds = {p.kind for p in find_problems(frame, inst, "1d")}
    assert "suspect" in kinds
    assert "zero_volume" in kinds
    assert "partial" in kinds


def test_spike_when_range_dwarfs_neighbors() -> None:
    inst = btc()
    stamps = [datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=i) for i in range(25)]
    frame = bars_frame(inst, stamps, source="binance", timeframe="1d")
    spike_ts = stamps[15]
    frame = frame.with_columns(
        pl.when(pl.col("ts_utc") == spike_ts).then(pl.lit(500.0)).otherwise(pl.col("high")).alias("high"),
        pl.when(pl.col("ts_utc") == spike_ts).then(pl.lit(1.0)).otherwise(pl.col("low")).alias("low"),
    )
    spikes = [p for p in find_problems(frame, inst, "1d") if p.kind == "spike"]
    assert any(p.ts_utc == spike_ts for p in spikes)
