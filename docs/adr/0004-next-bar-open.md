# ADR-0004: Causal close signal, fill at next-bar open

## Status

Accepted

## Context

A Strategy that sizes from bar `t`’s close and fills at that same close peeks: the signal used the close, and so did the trade. Mixing display TZ into fill timestamps would also shift which bar is “next.” Long, short, flip, and multi-name rebalance all share this clock; a per-family fill rule would hide look-ahead in the plugin.

## Decision

Separate **intent** from **execution**.

1. **Signal (close, causal).** `Strategy.targets` may use the Panel **up to and including this bar’s close**. It must not read bar `t+1`. Output is a Target (signed weights), never a Fill.
2. **Fill (next open).** The Broker **module** rebalances at **next bar open** to the previous close’s Target, and only when `|target − held|` exceeds `rebalance_band`. Fee is per-class on traded notional. Equity is marked on close (open/close gap included).
3. **Clocks.** Fill math uses `ts_utc` only. Display TZ is for reports and charts, not for which bar is next (ADR-0001).

This is one path for long, short, flip, and portfolio rebalance — not a special case per strategy family. `allow_short` is a Broker gate, not a Strategy type.

## Consequences

- A Target at `t` must not change Fills at open `t`; it can change Fills only at open `t+1`.
- Last bar of a window has a Target and no Fill (no next open). First bar can Fill from a prior Target outside the Metrics window only if that bar is in the Panel; v1 does not invent off-window opens.
- Warm-up / `ready_bars` still belongs to the Strategy; the Broker does not wait on Indicators.
- Tests that truncate the last bar must leave prior Fills unchanged.
