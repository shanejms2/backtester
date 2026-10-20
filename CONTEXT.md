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

One OHLCV interval. The timestamp is **bar open**, stored as timezone-aware **UTC**. Display-local time is not a stored column. Optional **`adj_close`**: Yahoo Adj Close for equities; crypto equals `close`. Null `adj_close` is treated as `close`. Broker fills still use raw `open`. Normalize rescales isolated 10× / 0.1× Yahoo level islands (close ~10× away from *both* neighbor medians) so a two-day decimal glitch is not stored as a crash; a real split or crash stays at the new level and is left alone.

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

Vendor name on each Request and each stored Bar: `yahoo`, `bhavcopy`, `alphavantage`, `binance`, `hyperliquid`, `dexscreener`, `geckoterminal`, …. `bhavcopy` is the NSE cash daily file (series EQ). The official zip is kept under `data/raw/bhavcopy/` before parsing. Close is the exchange close, not split-adjusted. Do not fan out one Request to every adapter.

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

Ingest, Normalize, Calendar, `BarReader`, Chart, and the JSON API talk to a `BarStore` **interface** (`write_batch`, `scan`, `coverage`, `list_coverage`). The live adapter is self-hosted **`ClickHouseStore`**. Postgres in compose is reserved for future user data, not bars. Legacy Hive Parquet under `data/bars/` is migration input only (`scripts/load_clickhouse_bars.sh`).

## Universe

The set of Instruments a Run trades. Listed in the Spec with per-name metadata (`class`, `calendar`, `fee_bps`, optional `min_adv`, optional `role`: `index_etf` / `commodity_etf` / `crypto` / `stock` / `inverse_etf`, optional `currency` default USD). Non-USD names convert via Spec `fx` (e.g. `INR: EQ:CCY:INR` from Yahoo `INR=X`, rupees per USD). The engine never branches on ticker (`if ticker == BTC`). Spec `cash_rate_id` is a T-bill *yield* overlay (not a Universe name, never clustered). A later plugin is a config row plus a plugin file.

## Panel

Bars for the Universe in long format (`ts_utc`, `instrument_id`, OHLCV, `adj_close`), aligned on timestamp, plus per-name metadata from the Spec. UTC storage clock only; display TZ is for reports and charts, not fill math (ADR-0001).

- **`clock: native`** (default): inner-join native `ts_utc` (same Timeframe on every name).
- **`clock: xnse`**: rows are NSE session days (XBOM holidays), stamped 15:30 IST, with that session's 09:15 IST open on the row.
- **`clock: nyse_1600`**: rows are NYSE session days only. `close` is the last valid price at or before 16:00 ET (ETF native 1d close; crypto last 1h bar whose close ≤ 16:00 ET). `open` on that row is the same session's 09:30 ET execution (ETF 1d open; crypto 1h covering 09:30) so the **next** row's open is the next-session fill (ADR-0004). Weekend crypto does not create a decision row. A closed name keeps a null-OHLC row; a null Target holds.

Loaded only through `BarReader`. Optional session/decision flags so a plugin can trade NYSE close while crypto bars still exist on weekends.

## Indicator

A causal function of one Instrument’s bars (or the Panel grouped by `instrument_id`). Returns **columns**, never weights. SMA, RSI, ATR, `ewm_std` live in the `backtester.ta` **module** so Strategies do not each reimplement them. See ADR-0006.

## Strategy

A causal function of the Panel **up to and including this bar’s close**, usually by composing Indicators. Output is a Target. No peeking at bar `t+1`. Cross-section math (vol, corr, clusters, hysteresis) lives in the plugin, not the engine. The Strategy **interface** is the **seam** (ADR-0005). Built-ins include `buy_hold`, `sma_cross`, `static`, `year_winner` (each year-end, hold the name with the best full prior calendar-year return) (constant `params.weights` map), `cap_mix` (each quarter, equal-weight the trailing-year leaders of each size sleeve), `periodic` (fixed weights reset on a calendar schedule; `every: quarter`), `guard` (a risk-on book, switched to a cash-heavy book when a guard Instrument closes below its slow average and back when it closes above a faster average; `rise_bars` also requires that slow average to be higher than it was that many sessions ago), and `prism` (calendar TSMOM, EWMA vol, hysteresis, monthly clusters, 12% vol target). `prism` `allow_cash` (default true) leaves leftover weight in T-bills; `allow_cash: false` rescales ON names to 100% and holds the last book when every trend is off.

## Target

Signed portfolio intent: `instrument_id → weight`. `0` is cash, `+1` full long, `-1` full short. Fractional and multi-name weights are allowed. Cash is leftover when gross is under 1. Not a Fill.

## Broker

Execution **module**. At **next bar open**, rebalance holdings to the previous close’s Target when `|target − held|` exceeds the band. Flat `rebalance_band` is the default (`sma_cross`). Proportional band is `threshold_i = max(band_floor, band_frac * |target_i|)`. `half_move` steps halfway to target; a target of **0** always fully exits. A **null** Target holds the last weight. Spec `drift: true` lets that weight follow price until the next non-null Target resets it (a quarterly 50/50). Default `drift: false` is constant-mix. A Run may set `drift` and override the Spec. Per-class fee on traded notional. Mark equity on close (plus open/close gap). Leftover cash earns `cash_weight * daily_tbill` from Spec `cash_rate_id` (Yahoo IRX close is an annualized percent; period return = `(close / 100 / 365) × calendar days` since the previous panel row so session clocks accrue weekends/holidays). Long, short, flip, and portfolio rebalance are the same path — not a special case per strategy family. See ADR-0004 and ADR-0007.

## Fill

A Broker trade at next-bar open: instrument, size, price, fee. Produced only when the rebalance band is exceeded. Strategies never emit Fills.

## Run

One Strategy + params + Universe + Timeframe + window + costs. The unit Metrics and Comparison measure.

## Spec

A YAML list of Runs that share the same Panel so Comparison is honest. Names Strategies by registry key, not by importing a class. Missing Coverage fails with the ingest command to run; the engine does not fetch (Ingest stays the collector).

## Metrics

Total return, CAGR, growth of $100 invested at the ready-bar start (`growth_100`), SIP $100/month on the first decision day of each calendar month in the Metrics window (`sip_contributed`, `sip_value`, `sip_multiple` = end / contributed, `sip_cagr` = money-weighted XIRR of each SIP day −100 and last day +ending wealth, `sip_max_drawdown` = peak-to-trough on the post-contribution SIP wealth series), annualized vol, Sharpe excess vs cash (`sharpe_excess`) and raw ret/vol (`sharpe_raw`), Sortino, Calmar, max drawdown (lump-sum equity, not SIP wealth), trades / fill count, win rate, turnover / annual turnover, mean gross, mean cash, best/worst calendar year, ending equity, versus a baseline Run (usually `buy_hold` on the same Universe/window). SIP is scored on each Run's aligned return series in Metrics/Compare — not inside a Strategy and not as extra Fills. `sip_cagr` is not lump-sum time-weighted CAGR (TWR of `r_t` is the same as lump-sum). Compare scores every Run from the latest ready timestamp on the Spec (the first bar the slowest Strategy is *ready*), not at listing.

## Comparison

Side-by-side Metrics (and optional monthly HTML dashboard) for Runs in one Spec. Honest only because they share the Panel and the ready-bar alignment. `strat compare --chart` writes a self-contained webpage: month-end $100 growth (lump-sum or SIP), stacked Target allocation with a month slider, and the per-name scorecard. Legend click hides a series; hover reads every name at that month.

## Tests

The **interface** of each module is the test surface. Pytest lives in `tests/` and uses JSON fixtures under `tests/fixtures/` — never live vendor APIs, never `.env` keys. Chart tests use `--no-open` and do not launch a browser.

Tests do **not** run on ingest or on save. They run when someone executes `pytest` locally, and automatically on GitHub Actions.

## CI

GitHub Actions workflow [`.github/workflows/ci.yml`](.github/workflows/ci.yml):

- **When:** push to `main`, pull request targeting `main`, or manual “Run workflow”.
- **What:** Python 3.12, `pip install -e ".[dev]"`, `ruff check`, `ruff format --check`, `mypy`, `pytest`.
- **Secrets:** none. `ALPHAVANTAGE_API_KEY` is empty in CI on purpose.
