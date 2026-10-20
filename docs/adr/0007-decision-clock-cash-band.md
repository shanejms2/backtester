# ADR-0007: Decision clock, adj_close, T-bill cash, proportional band

## Status

Accepted

## Context

PRISM (and any Strategy that mixes NYSE ETFs with 24/7 crypto) cannot size from native UTC midnights: a 1d ETF bar and a 1h crypto bar do not share `ts_utc`. Inner-joining those stamps drops the crypto name or invents a UTC-day that is not a New York session. Total-return signals need Yahoo Adj Close while fills must stay on raw open (ADR-0004). Idle cash is not a clustered sleeve; it should earn the T-bill yield. A flat rebalance band is the wrong hysteresis for large vs small Target weights, and a half-move with a forced full exit at 0 is a Broker concern, not a Strategy.

ADR-0005 left T-bill overlay unwired. This ADR wires it as a **cash return**, not as a dummy cluster member.

## Decision

1. **Decision clock.** Spec `clock: native` (default) keeps today's inner-join on native `ts_utc`. `clock: nyse_1600` builds the Panel on **NYSE session days only**. Per name, `close` is the last valid price at or before 16:00 America/New_York (ETF native 1d close; crypto last 1h bar whose close ≤ 16:00 ET). `open` is that session's 09:30 ET execution (ETF next-row 1d open / crypto 1h covering 09:30) so existing `weight.shift(1)` at open stays the fill path. Weekend crypto does not create a decision row. A closed name keeps a null-OHLC row; a **null Target holds** last weight. Weight `0` fully exits.

2. **`adj_close`.** Optional Bar / Parquet column. Null means `close`. Yahoo fills Adj Close. Crypto `adj_close = close`. Strategies may read `adj_close`; the Broker still fills on raw `open`.

3. **Cash overlay.** Spec `cash_rate_id` points at a yield series (Yahoo `^IRX`, close quoted as an annualized **percent**). The per-calendar-day factor is `close / 100 / 365`. Panel attachment multiplies by **calendar days since the previous panel stamp** (first stamp accrues 0) so `nyse_1600` / `xnse` still earn over weekends and holidays. The Broker adds `cash_weight * daily_tbill` (that period return) to the equity return. Gross still caps at 1; leftover is cash. The rate id is **not** a Universe name and is never clustered.

4. **Proportional band + half-move.** `threshold_i = max(band_floor, band_frac * |target_i|)` when either knob is set; otherwise flat `rebalance_band` (sma_cross default). When a trade triggers, `half_move` does `held += 0.5 * (target − held)`. Target `0` always fully exits.

5. **Metrics.** CAGR, ann. vol, Sharpe excess vs cash (`sharpe_excess`) and raw ret/vol (`sharpe_raw`), Sortino, Calmar, mean gross, mean cash, annual turnover, fill count, best/worst calendar year, in addition to the v1 totals. Alignment remains `ready_bars`.

6. **`ta.ewm_std(panel, half_life, col=…)`** is a causal Indicator, grouped `.over("instrument_id")`. **`static`** is a built-in Strategy (`params.weights` map) on the same registry seam.

## Consequences

- Mixed ETF 1d + crypto 1h Coverage is readable without resampling crypto to UTC daily.
- Truncating the last decision bar must not change prior Fills (ADR-0004 still holds).
- sma_cross Specs omit the new knobs and keep flat `rebalance_band`.
- IRX / BIL must not appear as clustered sleeve ids; cash is the leftover weight.
