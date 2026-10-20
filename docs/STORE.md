# Store

v1 persistence is Hive-partitioned Parquet (ZSTD) plus one embedded DuckDB file. There is no migration tool. `_init_catalog` runs `CREATE TABLE IF NOT EXISTS` when the store opens. Adding a column means updating that SQL and the write path together; existing files are not rewritten by a migrator.

The directory is `BACKTESTER_DATA_DIR` (`data/` by default, gitignored).

## Files

```
data/
  catalog.duckdb
  bars/asset_class={equity|crypto|dex}/venue={venue}/symbol={symbol}/timeframe={tf}/year={YYYY}/month={MM}/part-*.parquet
  snapshots/source={source}/chain={chain}/pair={pair}/year={YYYY}/month={MM}/part-*.parquet
```

`symbol` is path-safe: `/` and `:` become `-` (`BTC/USDT` → `BTC-USDT`). `year` and `month` are the UTC calendar of `ts_utc` (bar open), not the venue session date.

A read of one instrument and timeframe opens only the year/month folders that overlap the request. Parquet is columnar; a scan that needs `ts_utc` and `close` does not have to materialize every column until the caller collects.

## Bar columns

Defined once by `domain.empty_bars`. The store's `BAR_COLUMNS` is that column list.

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

Dedup key on write is `(instrument_id, timeframe, ts_utc)` within a source, last write wins. A partition rewrite reads existing parts, concatenates the new page, dedups, writes a new part, checks the row count, renames it into place, then deletes the old parts. The exclusive lock is `folder/.lock` via `fcntl.flock`. The temp file lives in `folder/.tmp`.

## DuckDB tables

Opened as `data/catalog.duckdb`. Statements take the store's `RLock`.

### instruments

Primary key `instrument_id`.

| Column | Notes |
| --- | --- |
| `instrument_id` | Primary key |
| `asset_class` | `equity`, `crypto`, or `dex` |
| `venue` | MIC, CCXT id, or chain |
| `symbol` | Ticker, CCXT symbol, or pair address |
| `session_tz` | Venue clock |
| `aliases` | JSON list |
| `alternatives` | JSON list of other DEX pools |
| `pair_created_at` | DEX pool birth. Coverage cannot start earlier |
| `base_token`, `quote_token` | DEX names |

### aliases

Primary key `alias_key` (lowercased). `instrument_id` points at `instruments`. Lookup is case-insensitive. Last register wins for an alias.

### coverage

No primary key and no unique constraint. Rows are inclusive UTC ranges already fetched for `(instrument_id, timeframe, source)`.

`update_coverage` loads the rows for that triple, merges overlaps in Python, deletes those rows, inserts the merged list, and commits. `drop_coverage` does the same after subtracting a range. Callers must not assume a single row per triple; the merge keeps disjoint intervals as separate rows.

### files

Primary key `path`. Records `bytes`, `row_count`, `min_ts`, `max_ts`, and `kind` (`bars` or snapshots) after a successful rename. Used to find scan paths and to decide compaction.

### snapshot_polls

Primary key `(instrument_id, source)`. Last successful PairSnapshot poll. Not a bar range. Ingest uses it so a snapshot source is not treated like OHLCV Coverage.

## Compaction and 1m retention

`compact` merges a partition when it has at least 8 part files or the average part is under about 1 MB. It rewrites one part and updates `files`.

`prune_hot_1m` deletes 1m part files for months that end before `now - hot_days`, and only when that instrument and source already have Coverage for native `1h` or `1d`. It then `drop_coverage`s that month so the catalog does not claim the minutes are still stored. Asking ingest for that old range again will fetch it.

## What the strategy engine sees

`BarReader.scan` filters `ts_utc` to the requested range and optional source. Old partitions without `adj_close` gain that column as `close` on read. The strategy engine never opens a Parquet path itself.
