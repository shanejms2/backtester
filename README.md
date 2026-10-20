# backtester

Coverage-driven market data engine and causal strategy runner. Collect stock and crypto bars as needed, normalize them into one schema, persist them in self-hosted ClickHouse, and read them through `BarReader`. Strategies run on that stored Panel. They do not fetch.

Storage is UTC only. Session clocks belong to the venue (NYSE in `America/New_York`, NSE in `Asia/Kolkata`, crypto 24/7 UTC). Display timezone is whatever IANA name you pass at read time.

Names and locked decisions: [CONTEXT.md](CONTEXT.md), [docs/adr/](docs/adr/). Layout and flows: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/CODEBASE_MAP.md](docs/CODEBASE_MAP.md).

## Installation

Python 3.12+.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # set ALPHAVANTAGE_API_KEY if you use Alpha Vantage
```

## Quick start

```bash
# Equities (default source: alphavantage when the env key is set, else yahoo)
backtester ingest AAPL --tf 1d --from 2024-01-01 --to 2024-03-01 --source yahoo

# CEX crypto
backtester ingest BTC/USDT --tf 1h --from 2024-06-01 --to 2024-06-08 --source binance --class crypto

# DEX bars: resolve on DexScreener, OHLCV from GeckoTerminal
backtester ingest PEPE --tf 1h --from 2024-06-01 --class dex --source geckoterminal

# Read in a display zone (always keeps ts_utc; adds ts_local)
backtester read EQ:XNAS:AAPL --tf 1d --from 2024-01-01 --to 2024-03-01 --tz Asia/Kolkata

backtester coverage EQ:XNAS:AAPL --tf 1d
backtester compact

# Visual QA (Plotly HTML in the browser; --no-open writes the file only)
backtester chart CRYPTO:HYPERLIQUID:HYPE/USDC --tf 1d --tz UTC
backtester chart --inventory

# Strategies on stored bars (ingest first; a hole prints the command to run)
backtester ta list
backtester strat list
backtester strat run examples/qqq-gld.yaml
backtester strat compare examples/qqq-gld.yaml --chart --no-open
```

Same commands work as `python -m backtester ...`.

## Strategies

A Spec is a YAML file: one Universe, one window, several Runs. Every Run shares the Panel. Metrics start at the latest `ready_bars` on the Spec, so a buy-and-hold comparison does not start before the slowest strategy is warm.

`clock` chooses the decision rows:

| Clock | Rows |
| --- | --- |
| `native` (default) | Inner join on stored `ts_utc`. Every name uses the Spec timeframe. |
| `nyse_1600` | NYSE session days. Close is the last price at or before 16:00 ET. Open on that row is 09:30 ET, so the next row's open is the fill. Crypto is read from 1h bars. |
| `xnse` | NSE session days, stamped 15:30 IST, open 09:15 IST. |

Fills happen at the next bar's raw `open`, and only when the weight change clears the rebalance band. A target of `0` exits. A null target holds. `cash_rate_id` (Yahoo `^IRX`, stored as `EQ:XNAS:IRX`) pays leftover cash `(close / 100 / 365) × calendar days` since the previous panel row (so session clocks accrue weekends). It is not a universe name.

`drift: true` lets weights follow price between targets. `drift: false` is constant-mix. A Run can override the Spec.

Built-in strategies: `buy_hold`, `cap_mix`, `guard`, `periodic`, `prism`, `sma_cross`, `static`, `year_winner`. Examples live in `examples/`.

## Tests

```bash
pytest
```

Tests use JSON fixtures under `tests/fixtures/`. They do not call vendors and they do not read `.env`. Chart and compare tests pass `--no-open`. GitHub Actions runs `ruff`, `mypy`, and `pytest` on Python 3.12 with an empty `ALPHAVANTAGE_API_KEY`.

## JSON API (public HTTPS)

Read-only JSON over stored bars. See [docs/API.md](docs/API.md).

```bash
# set BACKTESTER_API_KEY in .env, then:
docker compose up -d --build api
curl -sS http://127.0.0.1:8088/health
curl -sS -H "X-API-Key: $BACKTESTER_API_KEY" \
  "http://127.0.0.1:8088/v1/bars?instrument_id=EQ:XNAS:AAPL&tf=1d&from=2024-01-01&to=2024-03-01"
```

Public host: https://backtester-api.shanejames.dev (nginx + Certbot; steps in `docs/API.md`).

## Configuration

| Variable | Meaning |
| --- | --- |
| `ALPHAVANTAGE_API_KEY` | Alpha Vantage key. Never commit `.env`. |
| `BACKTESTER_TZ` | CLI default IANA zone when `--tz` is omitted (`UTC` if unset). |
| `BACKTESTER_DATA_DIR` | Hive root (`data/` by default, gitignored). |
| `BACKTESTER_HOT_1M_DAYS` | Retention window for dense 1m. |
| `BACKTESTER_AV_PREMIUM` | `1` if the AV key can use `outputsize=full` daily / crypto intraday. |
| `BACKTESTER_API_KEY` | Required for compose `api` / public HTTPS (`X-API-Key` header). |
| `BACKTESTER_API_MAX_BARS` | Max rows per `/v1/bars` response (default 10000). |

## Development

```bash
ruff check src tests
ruff format src tests
mypy
pytest
```

## More documentation

| Doc | What it answers |
| --- | --- |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Pipelines, seams, clocks, what not to split |
| [docs/CODEBASE_MAP.md](docs/CODEBASE_MAP.md) | Where each module lives and where to edit |
| [docs/CLI_FLOWS.md](docs/CLI_FLOWS.md) | What each command does, including errors |
| [docs/STORE.md](docs/STORE.md) | ClickHouse tables (bars, coverage, catalog) |
| [docs/API.md](docs/API.md) | Public HTTPS JSON API |
| [docs/DEVELOPER_GUIDE.md](docs/DEVELOPER_GUIDE.md) | Add a strategy, indicator, adapter, or test |
| [observability/README.md](observability/README.md) | Project Grafana / Loki / Alloy / Prometheus |
