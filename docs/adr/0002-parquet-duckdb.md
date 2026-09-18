# ADR-0002: Hive-partitioned Parquet (ZSTD) + DuckDB catalog

## Status

Accepted

## Context

The engine is local, as-needed, and append-mostly. OHLCV scans filter by instrument, timeframe, and time range. A always-on Postgres (or Timescale) server would add install, migrate, backup, and pooling cost without buying capability we lack on a single workstation. Airflow would add a DAG scheduler for a flow that is Coverage-first, not a nightly job grid.

## Decision

v1 persistence is **Hive-partitioned Parquet (ZSTD) + DuckDB catalog**.

Hive folders encode filter keys:

```
data/bars/asset_class=equity/venue=XNAS/symbol=AAPL/timeframe=1d/year=2024/month=01/*.parquet
data/bars/asset_class=crypto/venue=binance/symbol=BTC-USDT/timeframe=1h/year=2024/month=01/*.parquet
data/bars/asset_class=dex/venue=solana/symbol=PAIRADDR/timeframe=1h/year=2024/month=01/*.parquet
data/snapshots/source=dexscreener/chain=solana/pair=PAIRADDR/year=2026/month=09/*.parquet
```

A read of AAPL 1d for Jan 2024 opens that month’s files only. Parquet is columnar: a `BarReader` that needs `ts_utc, close` does not load `volume`. ZSTD plus dictionary encoding keeps low-cardinality columns small.

DuckDB is an embedded file, not a server. It stores `instruments`, `coverage`, and `files` (path, bytes, row_count, min/max `ts_utc`) and can SQL-scan Parquet in place.

The **Store interface** (`write_batch`, `scan`, `coverage`) is the **seam** for a later Postgres/Timescale **adapter**. Ingest must not import Parquet paths. This build does not implement Postgres.

Not Airflow: a Request asks for a range; Ingest fetches holes, then stops. Revisit a scheduler only if job topology becomes a product.

## Consequences

- Compaction merges tiny parts in a partition. Retention may drop old dense 1m once a native 1h/1d series exists; Coverage is updated so ingest does not refill deleted 1m.
- File lock per partition so two Requests cannot corrupt a write.
- Writes are stage → validate row counts → atomic rename → Coverage update.
- Gitignore `data/` (and `.env`).
