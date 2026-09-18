"""Store: Hive prune, compaction, retention, idempotent writes, Coverage."""

from __future__ import annotations

from datetime import UTC, datetime

from backtester.store.parquet_duckdb import ParquetDuckDBStore
from tests.conftest import aapl, bars_frame, utc


def test_write_and_scan_roundtrip(tmp_store: ParquetDuckDBStore) -> None:
    inst = aapl()
    tmp_store.upsert_instrument(inst)
    frame = bars_frame(inst, [utc(2024, 1, 16, 14, 30)], source="yahoo", timeframe="1d")
    result = tmp_store.write_batch(frame)
    assert result.rows_written == 1
    out = tmp_store.scan(inst.instrument_id, "1d", utc(2024, 1, 1), utc(2024, 2, 1)).collect()
    assert out.height == 1
    assert str(out["instrument_id"][0]) == inst.instrument_id


def test_partition_prune_skips_other_months(tmp_store: ParquetDuckDBStore) -> None:
    inst = aapl()
    tmp_store.upsert_instrument(inst)
    jan = bars_frame(inst, [utc(2024, 1, 16, 14, 30)], source="yahoo", timeframe="1d")
    feb = bars_frame(inst, [utc(2024, 2, 15, 14, 30)], source="yahoo", timeframe="1d")
    tmp_store.write_batch(jan)
    tmp_store.write_batch(feb)
    paths = tmp_store._bar_paths(inst.instrument_id, "1d", utc(2024, 1, 1), utc(2024, 1, 31, 23))
    assert paths
    assert all("month=01" in p.replace("\\", "/") for p in paths)
    assert all("month=02" not in p.replace("\\", "/") for p in paths)
    scanned = tmp_store.scan(inst.instrument_id, "1d", utc(2024, 1, 1), utc(2024, 1, 31, 23)).collect()
    assert scanned.height == 1
    assert scanned["ts_utc"][0].month == 1


def test_dedup_last_write_wins(tmp_store: ParquetDuckDBStore) -> None:
    inst = aapl()
    tmp_store.upsert_instrument(inst)
    ts = utc(2024, 1, 16, 14, 30)
    first = bars_frame(inst, [ts], source="yahoo", timeframe="1d")
    first = first.with_columns(first["close"] * 0 + 10.0)
    tmp_store.write_batch(first)
    second = bars_frame(inst, [ts], source="yahoo", timeframe="1d")
    second = second.with_columns(second["close"] * 0 + 12.0)
    tmp_store.write_batch(second)
    out = tmp_store.scan(inst.instrument_id, "1d", utc(2024, 1, 1), utc(2024, 2, 1)).collect()
    assert out.height == 1
    assert float(out["close"][0]) == 12.0


def test_coverage_merge(tmp_store: ParquetDuckDBStore) -> None:
    inst = aapl()
    tmp_store.update_coverage(inst.instrument_id, "1d", "yahoo", utc(2024, 1, 1), utc(2024, 1, 10))
    tmp_store.update_coverage(inst.instrument_id, "1d", "yahoo", utc(2024, 1, 10), utc(2024, 1, 20))
    ranges = tmp_store.coverage(inst.instrument_id, "1d", "yahoo")
    assert len(ranges) == 1
    assert ranges[0].start_utc == utc(2024, 1, 1)
    assert ranges[0].end_utc == utc(2024, 1, 20)


def test_compact_merges_parts(tmp_store: ParquetDuckDBStore) -> None:
    inst = aapl()
    tmp_store.upsert_instrument(inst)
    for day in range(2, 6):
        tmp_store.write_batch(bars_frame(inst, [utc(2024, 1, day, 14, 30)], source="yahoo", timeframe="1d"))
    folder = (
        tmp_store.root
        / "bars"
        / "asset_class=equity"
        / "venue=XNAS"
        / "symbol=AAPL"
        / "timeframe=1d"
        / "year=2024"
        / "month=01"
    )
    # Force extra tiny parts by writing then compacting.
    before = list(folder.glob("part-*.parquet"))
    # write_batch merges into one file per partition already; split by writing compact of copies.
    # Re-create multiple parts by copying isn't needed if write merges. Simulate leftovers:
    if len(before) == 1:
        data = before[0].read_bytes()
        (folder / "part-aaaa.parquet").write_bytes(data)
        (folder / "part-bbbb.parquet").write_bytes(data)
    removed = tmp_store.compact(inst.instrument_id)
    assert removed >= 1
    parts = list(folder.glob("part-*.parquet"))
    assert len(parts) == 1


def test_prune_old_1m_when_1d_exists(tmp_store: ParquetDuckDBStore) -> None:
    inst = aapl()
    tmp_store.upsert_instrument(inst)
    old = datetime(2020, 1, 15, 14, 30, tzinfo=UTC)
    tmp_store.write_batch(bars_frame(inst, [old], source="yahoo", timeframe="1m"))
    tmp_store.write_batch(bars_frame(inst, [old], source="yahoo", timeframe="1d"))
    tmp_store.update_coverage(inst.instrument_id, "1m", "yahoo", old, old)
    tmp_store.update_coverage(inst.instrument_id, "1d", "yahoo", old, old)
    removed = tmp_store.prune_hot_1m(hot_days=30, now=datetime(2024, 6, 1, tzinfo=UTC))
    assert removed >= 1
    leftover = tmp_store.scan(
        inst.instrument_id, "1m", datetime(2020, 1, 1, tzinfo=UTC), datetime(2020, 2, 1, tzinfo=UTC)
    ).collect()
    assert leftover.height == 0
    assert tmp_store.coverage(inst.instrument_id, "1m", "yahoo") == []


def test_instrument_alias_persisted(tmp_store: ParquetDuckDBStore) -> None:
    inst = aapl()
    tmp_store.upsert_instrument(inst)
    found = tmp_store.find_by_alias("aapl")
    assert found is not None
    assert found.instrument_id == inst.instrument_id
