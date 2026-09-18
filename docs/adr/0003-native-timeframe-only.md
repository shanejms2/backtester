# ADR-0003: Native Timeframe only

## Status

Accepted

## Context

Storing every derived bucket (5m, 15m, 1h) next to 1m multiplies disk and Coverage complexity. Vendors also differ: Yahoo 1m is ~7 days; Alpha Vantage has no native 4h; GeckoTerminal public history is ~180 days.

## Decision

Persist **native** Timeframes only. Do not store 5m and 15m and 1h if 1m exists. Resample on read with Polars `group_by_dynamic`.

Exception: if the vendor cannot supply 1m for the requested history, store the coarsest native series the vendor actually has and record that in Coverage.

Do not rebucket stored daily bars because display TZ changed. Explicit `resample` with a display `tz` produces a **derived** series from a finer native Timeframe, never a rewrite of stored `1d`.

Yahoo has no native 4h; Alpha Vantage has no native 4h. Those Requests either fetch `1h` and resample on read, or use a vendor that native-supports `4h` (CCXT, GeckoTerminal).

## Consequences

- Coverage is per `(Instrument, Timeframe, source)` of what is on disk, not of every derived view.
- Hot / warm / cold retention can drop old 1m after a native 1h/1d series exists.
- `BarReader` is the **seam** for derived series; ingest stays dumb about resampling.
