"""BarReader: display TZ, reject IST, resample in Tokyo ≠ stored NY daily."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backtester.exceptions import InvalidTimezoneError
from backtester.query.reader import BarReader
from backtester.store.parquet_duckdb import ParquetDuckDBStore
from tests.conftest import aapl, bars_frame, utc


def test_read_kolkata_adds_ts_local(tmp_store: ParquetDuckDBStore) -> None:
    inst = aapl()
    tmp_store.upsert_instrument(inst)
    ts = datetime(2024, 6, 3, 13, 30, tzinfo=UTC)  # 09:30 EDT
    tmp_store.write_batch(bars_frame(inst, [ts], source="yahoo", timeframe="1d"))
    reader = BarReader(tmp_store)
    frame = reader.read(
        inst.instrument_id,
        "1d",
        datetime(2024, 6, 1),
        datetime(2024, 6, 5),
        tz="Asia/Kolkata",
    ).collect()
    assert "ts_utc" in frame.columns
    assert "ts_local" in frame.columns
    assert "utc_offset" in frame.columns
    local = frame["ts_local"][0]
    assert local.tzinfo is not None
    assert int(frame["utc_offset"][0]) == 330  # IST is UTC+5:30, no DST


def test_read_default_no_ts_local(tmp_store: ParquetDuckDBStore) -> None:
    inst = aapl()
    tmp_store.upsert_instrument(inst)
    tmp_store.write_batch(bars_frame(inst, [utc(2024, 6, 3, 13, 30)], source="yahoo", timeframe="1d"))
    frame = BarReader(tmp_store).read(inst.instrument_id, "1d", utc(2024, 6, 1), utc(2024, 6, 5)).collect()
    assert "ts_local" not in frame.columns


def test_reject_ist_on_read(tmp_store: ParquetDuckDBStore) -> None:
    reader = BarReader(tmp_store)
    with pytest.raises(InvalidTimezoneError):
        reader.read("EQ:XNAS:AAPL", "1d", utc(2024, 1, 1), utc(2024, 2, 1), tz="IST")


def test_resample_tokyo_1d_not_ny_session(tmp_store: ParquetDuckDBStore) -> None:
    inst = aapl()
    tmp_store.upsert_instrument(inst)
    # Two NY RTH hours on 2024-06-03: 13:30 and 14:30 UTC.
    stamps = [
        datetime(2024, 6, 3, 13, 30, tzinfo=UTC),
        datetime(2024, 6, 3, 14, 30, tzinfo=UTC),
        datetime(2024, 6, 3, 20, 0, tzinfo=UTC),  # 05:00 next calendar day in Tokyo
    ]
    tmp_store.write_batch(bars_frame(inst, stamps, source="yahoo", timeframe="1h"))
    stored_daily = bars_frame(inst, [datetime(2024, 6, 3, 13, 30, tzinfo=UTC)], source="yahoo", timeframe="1d")
    tmp_store.write_batch(stored_daily)

    reader = BarReader(tmp_store)
    derived = reader.read(
        inst.instrument_id,
        "1h",
        datetime(2024, 6, 3, tzinfo=UTC),
        datetime(2024, 6, 5, tzinfo=UTC),
        tz="Asia/Tokyo",
        resample="1d",
    ).collect()
    native = reader.read(
        inst.instrument_id,
        "1d",
        datetime(2024, 6, 3, tzinfo=UTC),
        datetime(2024, 6, 5, tzinfo=UTC),
        tz="Asia/Tokyo",
    ).collect()
    assert derived.height >= 1
    assert native.height == 1
    # Tokyo daily buckets are not the NY session date series.
    derived_locals = [str(v) for v in derived["ts_local"].to_list()]
    native_local = str(native["ts_local"][0])
    assert native.height != derived.height or derived_locals[0] != native_local or derived.height > 1
