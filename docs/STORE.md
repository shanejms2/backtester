# Store

The engine store is **self-hosted ClickHouse** (`ClickHouseStore`). Schema is created on connect (`_ensure_schema`) and by [`infra/clickhouse/init/01_schema.sql`](../infra/clickhouse/init/01_schema.sql) for fresh compose volumes.

Connection: `CLICKHOUSE_HOST`, `CLICKHOUSE_PORT`, `CLICKHOUSE_USER`, `CLICKHOUSE_PASSWORD`, `CLICKHOUSE_DATABASE` (default `backtester`).

`BACKTESTER_DATA_DIR` is only for local vendor caches (e.g. bhavcopy under `data/raw/`), not bars.

## Tables

| Table | Engine | Role |
| --- | --- | --- |
| `bars` | `ReplacingMergeTree` | OHLCV; `ORDER BY (instrument_id, timeframe, source, ts_utc)` |
| `coverage` | `MergeTree` | Inclusive UTC ranges already fetched |
| `instruments` | `ReplacingMergeTree` | Catalog records |
| `aliases` | `ReplacingMergeTree` | Lowercased alias → `instrument_id` |
| `snapshots` | `ReplacingMergeTree` | DEX PairSnapshots |
| `snapshot_polls` | `ReplacingMergeTree` | Last snapshot poll per instrument/source |

## Bar columns

Defined once by `domain.empty_bars`.

| Column | Type | Meaning |
| --- | --- | --- |
| `instrument_id` | string | Stable id, `EQ:XNAS:AAPL` |
| `source` | string | Vendor that produced the row |
| `timeframe` | string | Native timeframe token |
| `ts_utc` | timestamp µs, UTC | Bar open |
| `open`, `high`, `low`, `close`, `volume` | float | Raw OHLC and volume |
| `is_partial` | bool | Last live bar before the interval closes |
| `quality` | string | `ok` or `suspect` |
| `adj_close` | float | Yahoo adjusted close. Null is treated as `close` |

`ts_local` is not stored.

Dedup on write is `(instrument_id, timeframe, ts_utc)` last-write-wins (Python), then `ReplacingMergeTree` + `FINAL` on read for the sort key that includes `source`.

## Coverage

Same merge/subtract semantics as v1: load intervals for a triple, merge in Python, delete that triple's rows (`mutations_sync=1`), insert the result. Callers must not assume a single row per triple.

## Compaction and 1m retention

`compact` is a no-op (ClickHouse merges parts).

`prune_hot_1m` deletes 1m bars older than `hot_days` when the same instrument/source already has Coverage for native `1h` or `1d`, then `drop_coverage`s that month.

## What the strategy engine sees

`BarReader.scan` filters `ts_utc` to the requested range and optional source. The strategy engine never talks to ClickHouse except through `BarStore`.

## Migration from Parquet + DuckDB

1. `docker compose up -d` (ClickHouse + Postgres).
2. `bash scripts/load_clickhouse_bars.sh` if bars are still only on disk as Parquet.
3. `pip install -e ".[migrate]"` then `python scripts/migrate_duckdb_catalog_to_clickhouse.py`.
4. Confirm with `clickhouse-client` counts on `bars` / `coverage` / `instruments`.
