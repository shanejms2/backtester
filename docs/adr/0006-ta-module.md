# ADR-0006: TA module is Indicators, not Strategies

## Status

Accepted

## Context

If every Strategy inlines SMA/RSI/Wilder smoothing, TA bugs hide in the plugin and plugins diverge. Wrapping pandas-ta or TA-Lib would pull a C extension (CI is pip-only) and a pandas **adapter** into a Polars Panel. Classic Ichimoku *displacement* plots span A/B into the future; that is display, not a causal column. A two-name Panel that rolls without grouping leaks one Instrument’s window into another.

## Decision

Deep **module** `backtester.ta`: Indicators, not Strategies. Strategies compose Indicator columns into signed Targets. TA never sees fees, bands, or shorts.

Locked:

- **Polars-native.** No pandas-ta, no TA-Lib C extension. Rolling ops are `.over("instrument_id")`.
- **Causal.** At bar `t` an Indicator may use that bar’s OHLCV and history through `t`. Never `t+1`. Ichimoku displacement is **not** in this module.
- **Warm-up is NaN**, not a partial window, unless the function documents otherwise (EMA seed). `ready_bars` on a Strategy is at least the max Indicator period it uses.
- **Public functions**, not an Indicator plugin registry. The **seam** is the **module**. `ta.sma(panel, length=20, col="close") → pl.Series` (and siblings); Strategies join named columns. A thin `ta.available()` list backs `backtester ta list` and the project skill.

v1 catalog is popular indicators (trend, momentum, volatility, volume, causal fractal swings), not the TA-Lib dump. Not in v1: candlestick pattern encyclopedia, Ichimoku, Parabolic SAR, volume profile.

Do not call `~/.cursor/skills/strategy-backtest` or `swing-structure` scripts from this repo. Same *type* as the desk swing skill; computed on a stored Panel, not live DexScreener.

## Consequences

- An RSI Strategy that inlines Wilder smoothing hides TA bugs in the plugin — Indicators concentrate math; Strategies only map columns to weights.
- Truncating the last bar must not change the previous Indicator value (no look-ahead). Name A’s RSI is unchanged if name B’s closes are scrambled (group leak).
- Adding an Indicator later is a new function in the **module**, not a registry key and not a Strategy.
- Tests own causality, per-`instrument_id` grouping, and NaN warm-up; they do not import journal scripts.
