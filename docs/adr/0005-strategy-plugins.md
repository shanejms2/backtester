# ADR-0005: Strategy plugins via registry, not a switch

## Status

Accepted

## Context

An engine that `if name == "sma_cross"` (or imports SMA by name) makes every new Strategy an engine change. Journal books (V3, V5 overlay, V6 clustered TSMOM, swing-structure, alloc presets) are *types* the engine must host later; shipping them as built-ins would freeze one research stack into the **module**. One built-in Strategy would leave the **seam** hypothetical.

## Decision

The Strategy **interface** is the **seam**. The engine talks only to that **interface** plus a name → class registry.

- `Strategy`: `name`, `param_model` (pydantic; unknown keys fail fast), `ready_bars(params)`, `targets(panel, params) →` columns `ts_utc`, `instrument_id`, `weight`.
- Registry: built-ins register on import. Extra plugins load via `importlib` from `spec.plugins` (module path or file). Adding a Strategy is a new file + `@register("my_name")`. The engine never imports SMA by name.
- Thin **adapters**: each built-in Strategy file. Cross-section math (vol, corr, clusters, hysteresis) stays in the plugin.

v1 ships **two seam-provers only** (two make the **seam** real):

- `buy_hold` — `1/N` hold (single-name and multi-name)
- `sma_cross` — `side: long_only | long_short` so signed Targets are exercised; implemented with `ta.sma`, not a private rolling mean

Do **not** port Crypto journal books (V3 / V5 overlay / V6 clustered TSMOM, swing-structure, alloc presets) and do not call `~/.cursor/skills/strategy-backtest/scripts/*.py`. Those remain plugins that can import `backtester.ta` and emit Targets onto the same Broker later.

## Consequences

- Unknown registry name / bad params fail fast at Spec load, not mid-Run.
- A `sma_cross()` that also computed Sharpe would hide fill bugs in the Indicator; Broker concentrates execution; Strategy stays replaceable.
- Journal-class Strategies get knobs on Panel / Spec / Broker (Universe metadata, signed fractional Targets, decision flags, rebalance band), not hardcoded tickers.
- Not wired in v1 (plugins may still emit weights; Broker ignores what it does not model): funding, IM/governor rungs, futures contract rounding, T-bill overlay as a dummy cluster.
