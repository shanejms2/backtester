# Domain language

Names used by the market data engine. Architecture work should keep using these words.

## Instrument

A tradable series with a **stable internal id**, not a vendor ticker.

Examples:

- `EQ:XNAS:AAPL`
- `CRYPTO:BINANCE:BTC/USDT`
- `CRYPTO:HYPERLIQUID:HYPE/USDC`
- `DEX:solana:<pairAddress>`

The Catalog maps vendor tickers, aliases, contract addresses, and DexScreener URLs onto this id. Ticker reuse and renames do not change the id. Delisted names stay readable if files exist.

## Bar

One OHLCV interval. The timestamp is **bar open**, stored as timezone-aware **UTC**. Display-local time is not a stored column.

## Timeframe

Canonical tokens: `1m`, `5m`, `15m`, `30m`, `1h`, `4h`, `1d`.

Store **native** Timeframes only. Do not persist 5m, 15m, and 1h just because 1m exists. Resample on read. If a vendor cannot supply 1m for the requested history, store the coarsest native series it actually has and record that in Coverage.

## Session

When an Instrument is expected to print bars. Tied to the **venue** clock:

- US equity: `America/New_York` (RTH / ETH). Daily date is the NY session date, not the UTC date of the close.
- Crypto and DEX: 24/7 UTC.

Session is not the user’s display timezone.

## Display timezone

An IANA name used only when presenting or (optionally) resampling on read (`Asia/Kolkata`, `UTC`, `Europe/London`, …). Independent of Session and of storage. Never stored on Parquet. Abbreviations (`IST`, `EDT`) and fixed offsets (`UTC-4`) are rejected.

## Coverage

Inclusive UTC ranges already fetched and persisted for `(Instrument, Timeframe, source)`. Ingest subtracts Coverage from a Request and only fetches holes.

## Gap

- **Expected**: weekend, NYSE holiday, early close, DST spring-forward skip on a venue clock. Do not refetch these.
- **Unexpected**: missing mid-session bar (halt, drop, vendor hole). Visible when comparing Session to stored Bars.

Crypto weekends are expected **bars**, not Gaps.

## Source

Vendor name on each Request and each stored Bar: `yahoo`, `alphavantage`, `binance`, `hyperliquid`, `dexscreener`, `geckoterminal`, …. Do not fan out one Request to every adapter.

## PairSnapshot

DexScreener current-state quote (price, liquidity, trailing volume windows). **Not** a Bar. Do not synthesize OHLC from `priceChange.h24`.

## Request

“I need Instrument X, Timeframe T, from A to B.” Ingest fetches only what Coverage lacks.

## BarReader

Query **interface** for stored Bars. Always returns `ts_utc`. An optional IANA `tz` adds `ts_local`. Optional `resample` is a derived series and never rewrites stored Timeframes. Future backtests should consume this, not adapters.

## Store seam

Ingest, Normalize, Calendar, and `BarReader` talk to a `BarStore` **interface** (`write_batch`, `scan`, `coverage`). v1 ships `ParquetDuckDBStore`. A later Postgres/Timescale **adapter** is out of scope for this build.

## Tests

The **interface** of each module is the test surface. Pytest lives in `tests/` and uses JSON fixtures under `tests/fixtures/` — never live vendor APIs, never `.env` keys.

Tests do **not** run on ingest or on save. They run when someone executes `pytest` locally, and automatically on GitHub Actions.

## CI

GitHub Actions workflow [`.github/workflows/ci.yml`](.github/workflows/ci.yml):

- **When:** push to `main`, pull request targeting `main`, or manual “Run workflow”.
- **What:** Python 3.12, `pip install -e ".[dev]"`, `ruff check`, `ruff format --check`, `mypy`, `pytest`.
- **Secrets:** none. `ALPHAVANTAGE_API_KEY` is empty in CI on purpose.
