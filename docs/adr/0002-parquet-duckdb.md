# ADR-0002: Store persistence

## Status

Superseded for the engine store by self-hosted **ClickHouse**. Hive Parquet + DuckDB was v1; the `BarStore` seam remains.

## Context

The engine is local/VPS, as-needed, and append-mostly. OHLCV scans filter by instrument, timeframe, and time range. Coverage-driven ingest needs a small catalog (instruments, aliases, coverage). A always-on Postgres for bars would add cost without matching columnar scan speed. Airflow is out of scope for hole-filling Requests.

## Decision (current)

**ClickHouse** (`ClickHouseStore`) holds:

- `bars` — OHLCV (`ReplacingMergeTree`)
- `coverage`, `instruments`, `aliases`
- `snapshots`, `snapshot_polls`

Connection settings: `CLICKHOUSE_*` in `.env`. Compose publishes ClickHouse on localhost only. Postgres remains reserved for future user data, not bars.

`BarStore` (`write_batch`, `scan`, `coverage`, …) stays the seam. Ingest must not import table DDL. `compact` is a no-op (server merges parts).

## Decision (historical v1)

v1 used Hive-partitioned Parquet (ZSTD) under `data/bars/` plus `data/catalog.duckdb` for metadata. That layout may remain on disk as a cold copy after migration; the CLI no longer opens it. The `ParquetDuckDBStore` Python module was removed after cutover; use `scripts/load_clickhouse_bars.sh` and `scripts/migrate_duckdb_catalog_to_clickhouse.py` for one-shot loads.

## Consequences

- One always-on ClickHouse process (memory-capped in compose).
- Coverage merge/drop still runs in Python; mutations use `mutations_sync=1`.
- `prune_hot_1m` deletes old 1m rows via `ALTER DELETE` when 1h/1d coverage exists.
- Migrate catalog once with `scripts/migrate_duckdb_catalog_to_clickhouse.py`. Bars load via `scripts/load_clickhouse_bars.sh` or ingest.
