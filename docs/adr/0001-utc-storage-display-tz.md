# ADR-0001: UTC storage, venue Session, display TZ at read

## Status

Accepted

## Context

OHLCV timestamps are the usual footgun: vendors emit naive strings, venue sessions follow a local clock with DST, and users want to see (or resample) bars in their own zone. Mixing those three clocks corrupts Coverage, Hive partitions, and later backtests.

## Decision

Three clocks, never mixed:

1. **Storage (invariant).** Store timezone-aware UTC only. `ts_utc` is bar open. Never rewrite Parquet because a user wants IST or Tokyo.
2. **Session (venue, ingest / gaps).** US equity uses `America/New_York`. Crypto / DEX uses UTC 24/7. Future venues get their own Session zone; still stored as UTC.
3. **Display (user, read path only).** `BarReader.read(tz=...)` and CLI `--tz` accept IANA names via `zoneinfo.ZoneInfo`. Always keep `ts_utc`. When `tz` is set, add `ts_local` (zone-aware) and `utc_offset` minutes so DST is visible. Default `tz=None` is UTC-only (no `ts_local`). CLI default is `UTC` or env `BACKTESTER_TZ`; never guess the machine zone if unset.

Reject fixed offsets (`UTC-4`, `GMT+5:30`) and abbreviations (`EDT`, `IST`, `EST`) with a hint to the IANA name. Unknown IANA names fail fast (`ZoneInfoNotFoundError`); do not silently fall back.

Do **not** rebucket stored daily bars when display TZ changes. AAPL `1d` is still the NY session. Rebucketing (`resample="1d"` with `tz="Asia/Tokyo"`) is explicit and produces a **derived** series from a finer native Timeframe.

Vendor ingest still normalizes *into* UTC. Alpha Vantage equity timestamps are naive Eastern strings: parse as `America/New_York`, then UTC. The last bar of a live Request is `is_partial` until the interval closes.

## Consequences

- Hive `year=` / `month=` partitions follow UTC of `ts_utc`.
- Request ranges given in the user’s zone convert to UTC before Coverage / scan.
- DST spring-forward skips a local display hour; fall-back duplicates one. `ts_local` must be zone-aware so fall-back is not collapsed. Native bar alignment does not change.
- Tests own 2025-03-09 / 2025-11-02 in NY **and** in a display zone with its own DST.
