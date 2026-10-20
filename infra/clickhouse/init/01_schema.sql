-- Self-hosted ClickHouse is the BarStore. Bars + catalog + snapshots live here.

CREATE DATABASE IF NOT EXISTS backtester;

CREATE TABLE IF NOT EXISTS backtester.bars
(
    instrument_id String,
    source String,
    timeframe String,
    ts_utc DateTime64(6, 'UTC'),
    open Float64,
    high Float64,
    low Float64,
    close Float64,
    volume Float64,
    is_partial Bool,
    quality String,
    adj_close Nullable(Float64)
)
ENGINE = ReplacingMergeTree
PARTITION BY (timeframe, toYYYYMM(ts_utc))
ORDER BY (instrument_id, timeframe, source, ts_utc);

CREATE TABLE IF NOT EXISTS backtester.coverage
(
    instrument_id String,
    timeframe String,
    source String,
    start_utc DateTime64(6, 'UTC'),
    end_utc DateTime64(6, 'UTC')
)
ENGINE = MergeTree
ORDER BY (instrument_id, timeframe, source, start_utc);

CREATE TABLE IF NOT EXISTS backtester.instruments
(
    instrument_id String,
    asset_class String,
    venue String,
    symbol String,
    session_tz String,
    aliases String,
    alternatives String,
    pair_created_at Nullable(DateTime64(6, 'UTC')),
    base_token Nullable(String),
    quote_token Nullable(String)
)
ENGINE = ReplacingMergeTree
ORDER BY instrument_id;

CREATE TABLE IF NOT EXISTS backtester.aliases
(
    alias_key String,
    instrument_id String
)
ENGINE = ReplacingMergeTree
ORDER BY alias_key;

CREATE TABLE IF NOT EXISTS backtester.snapshots
(
    source String,
    chain_id String,
    dex_id String,
    pair_address String,
    base_symbol String,
    quote_symbol String,
    base_address Nullable(String),
    quote_address Nullable(String),
    price_usd Nullable(Float64),
    price_native Nullable(Float64),
    liquidity_usd Nullable(Float64),
    volume_m5 Nullable(Float64),
    volume_h1 Nullable(Float64),
    volume_h6 Nullable(Float64),
    volume_h24 Nullable(Float64),
    price_change_h24 Nullable(Float64),
    fdv Nullable(Float64),
    market_cap Nullable(Float64),
    pair_created_at Nullable(DateTime64(6, 'UTC')),
    polled_at_utc DateTime64(6, 'UTC'),
    quality String,
    url Nullable(String)
)
ENGINE = ReplacingMergeTree
PARTITION BY toYYYYMM(polled_at_utc)
ORDER BY (source, chain_id, pair_address, polled_at_utc);

CREATE TABLE IF NOT EXISTS backtester.snapshot_polls
(
    instrument_id String,
    source String,
    polled_at_utc DateTime64(6, 'UTC')
)
ENGINE = ReplacingMergeTree
ORDER BY (instrument_id, source);
