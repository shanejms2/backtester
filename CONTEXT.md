# Domain language

Names used by the market data engine and the strategy engine. Architecture work should keep using these words.

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

## Chart

A **view** over `BarReader` and Coverage, not a stored series. Inspect lives in `backtester.inspect` (problem flags are pure functions; Plotly only renders). HTML is written under `$TMPDIR` and is not part of the Hive store.

- Series: `backtester chart CRYPTO:HYPERLIQUID:HYPE/USDC --tf 1d --tz UTC` — candlesticks + volume. Default range is the Coverage span. Header is first→last bar in the display TZ.
- Inventory: `backtester chart --inventory` — one lane per Coverage row (`list_coverage` on the Store **interface**).
- `--no-open` writes the file and prints the path (tests/CI). `--tz` is IANA-only; `IST`/`EDT` fail fast.
- Red spans are **unexpected Gaps**. Markers: `quality=suspect`, `is_partial`, `volume<=0`, range spikes vs rolling median.
- Native series above ~20k bars may be plotted as a coarser derived Timeframe; the title says so. HYPE 1d/4h stay native.

## Store seam

Ingest, Normalize, Calendar, `BarReader`, and Chart talk to a `BarStore` **interface** (`write_batch`, `scan`, `coverage`, `list_coverage`). v1 ships `ParquetDuckDBStore`. A later Postgres/Timescale **adapter** is out of scope for this build.

## Universe

The set of Instruments a Run trades. Listed in the Spec with per-name metadata (`class`, `calendar`, `fee_bps`, optional `min_adv`). The engine never branches on ticker (`if ticker == BTC`). A later plugin is a config row plus a plugin file.

## Panel

Bars for the Universe in long format (`ts_utc`, `instrument_id`, OHLCV), aligned on timestamp (inner join), plus per-name metadata from the Spec. UTC storage clock only; display TZ is for reports and charts, not fill math (ADR-0001). Optional session/decision flags so a plugin can trade NYSE close while crypto bars still exist on weekends. Loaded only through `BarReader`.

## Indicator

A causal function of one Instrument’s bars (or the Panel grouped by `instrument_id`). Returns **columns**, never weights. SMA, RSI, ATR live in the `backtester.ta` **module** so Strategies do not each reimplement them. See ADR-0006.

## Strategy

A causal function of the Panel **up to and including this bar’s close**, usually by composing Indicators. Output is a Target. No peeking at bar `t+1`. Cross-section math (vol, corr, clusters, hysteresis) lives in the plugin, not the engine. The Strategy **interface** is the **seam** (ADR-0005).

## Target

Signed portfolio intent: `instrument_id → weight`. `0` is cash, `+1` full long, `-1` full short. Fractional and multi-name weights are allowed. Cash is leftover when gross is under 1. Not a Fill.

## Broker

Execution **module**. At **next bar open**, rebalance holdings to the previous close’s Target when `|target − held|` exceeds `rebalance_band`. Per-class fee on traded notional. Mark equity on close (plus open/close gap). Long, short, flip, and portfolio rebalance are the same path — not a special case per strategy family. See ADR-0004.

## Fill

A Broker trade at next-bar open: instrument, size, price, fee. Produced only when the rebalance band is exceeded. Strategies never emit Fills.

## Run

One Strategy + params + Universe + Timeframe + window + costs. The unit Metrics and Comparison measure.

## Spec

A YAML list of Runs that share the same Panel so Comparison is honest. Names Strategies by registry key, not by importing a class. Missing Coverage fails with the ingest command to run; the engine does not fetch (Ingest stays the collector).

## Metrics

Total return, max drawdown, trades, win rate, turnover, ending equity, versus a baseline Run (usually `buy_hold` on the same Universe/window). Align the baseline at the first bar the Strategy is *ready* (Indicators warm), not at listing.

## Comparison

Side-by-side Metrics (and optional equity overlay) for Runs in one Spec. Honest only because they share the Panel and the ready-bar alignment.

## Tests

The **interface** of each module is the test surface. Pytest lives in `tests/` and uses JSON fixtures under `tests/fixtures/` — never live vendor APIs, never `.env` keys. Chart tests use `--no-open` and do not launch a browser.

Tests do **not** run on ingest or on save. They run when someone executes `pytest` locally, and automatically on GitHub Actions.

## CI

GitHub Actions workflow [`.github/workflows/ci.yml`](.github/workflows/ci.yml):

- **When:** push to `main`, pull request targeting `main`, or manual “Run workflow”.
- **What:** Python 3.12, `pip install -e ".[dev]"`, `ruff check`, `ruff format --check`, `mypy`, `pytest`.
- **Secrets:** none. `ALPHAVANTAGE_API_KEY` is empty in CI on purpose.
