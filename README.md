# backtester

Coverage-driven market data engine. Collect stock and crypto bars **as needed**, normalize them into one schema, persist at good size, and read them through `BarReader` so a backtest module can plug in later without touching ingest.

Storage is **UTC only**. Session clocks belong to the venue (NYSE in `America/New_York`; crypto 24/7 UTC). Display timezone is whatever IANA name you pass at read time.

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
```

Same commands work as `python -m backtester ...`.

## Configuration

| Variable | Meaning |
| --- | --- |
| `ALPHAVANTAGE_API_KEY` | Alpha Vantage key. Never commit `.env`. |
| `BACKTESTER_TZ` | CLI default IANA zone when `--tz` is omitted (`UTC` if unset). |
| `BACKTESTER_DATA_DIR` | Hive root (`data/` by default, gitignored). |
| `BACKTESTER_HOT_1M_DAYS` | Retention window for dense 1m. |
| `BACKTESTER_AV_PREMIUM` | `1` if the AV key can use `outputsize=full` daily / crypto intraday. |

## Development

```bash
ruff check src tests
ruff format src tests
mypy
pytest
```

See [CONTEXT.md](CONTEXT.md) for domain names and [docs/adr/](docs/adr/) for locked decisions.
