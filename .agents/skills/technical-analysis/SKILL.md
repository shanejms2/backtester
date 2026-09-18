---
name: technical-analysis
description: >-
  Computes causal Polars Indicators from backtester.ta (SMA, EMA, MACD, ADX,
  Supertrend, Aroon, RSI, Stochastic, CCI, ATR, Bollinger, Keltner, Donchian,
  OBV, MFI, CMF, session VWAP, fractal swings, HH/HL/LH/LL). Use when the user
  or a plugin needs popular TA, which indicators exist, or to compose signals
  into Target weights.
---

# Technical analysis (`backtester.ta`)

Only implementation: `backtester.ta`. Do **not** call `~/.cursor/skills/strategy-backtest` or `swing-structure` scripts from this repo.

Indicators return **columns**, never weights or Fills. A Strategy maps those columns to a Target; the Broker still fills at **next-bar open**.

```python
from backtester import ta
import polars as pl

ta.available()  # catalog names
sma20 = ta.sma(panel, length=20, col="close")  # pl.Series, warm-up is null
```

Panel is long format (`ts_utc`, `instrument_id`, OHLCV). All rolling ops are grouped by `instrument_id`. Causal: bar `t` uses OHLCV through `t` only. EMA / Wilder averages document an SMA seed; other windows are null until full.

## Catalog

Same names as `ta.available()`:

| Group | Functions |
|---|---|
| Trend | `sma`, `ema`, `wma`, `macd` (macd/signal/hist), `adx` (adx/+DI/−DI), `supertrend`, `aroon` |
| Momentum | `rsi` (Wilder), `stoch`, `stoch_rsi`, `cci`, `willr`, `roc`, `mom` |
| Volatility | `atr`, `bbands` (mid/upper/lower/%B/width), `keltner`, `donchian`, `stdev` |
| Volume | `obv`, `mfi`, `cmf`, `vwap` (resets on `session_date` or `calendar` day, else UTC date) |
| Structure | `swing_high`, `swing_low`, `swing_labels`, `swings` — fractal `left`/`right`; labels on the **confirmation** bar |

Multi-output helpers return a DataFrame of columns to `with_columns`.

## Composition (weights, not fills)

SMA cross — long when fast > slow:

```python
fast = ta.sma(panel, length=20)
slow = ta.sma(panel, length=50)
weight = pl.when(fast > slow).then(1.0).otherwise(0.0)
```

RSI bands — long below 30, flat above 70 (no position sizing here):

```python
r = ta.rsi(panel, length=14)
weight = pl.when(r < 30).then(1.0).when(r > 70).then(0.0).otherwise(None)
```

ATR / Supertrend trailing as a signed Target:

```python
st = ta.supertrend(panel, length=10, multiplier=3.0)
weight = pl.col("direction")  # after panel.with_columns(st); +1 / -1
```

Join Series back with `panel.with_columns(ta.sma(panel, 20).alias("sma20"))`. Cross-section math stays in the Strategy plugin.

## Do not

- Treat an Indicator as a Strategy or emit Fills from TA.
- Peek at bar `t+1`, use Ichimoku displacement, or pull pandas-ta / TA-Lib.
- Reimplement RSI/SMA in a plugin when `backtester.ta` already has it.
