#!/usr/bin/env bash
# One-shot: copy Hive Parquet under data/bars into ClickHouse backtester.bars.
# Source of truth stays on disk; catalog.duckdb is not loaded.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -d data/bars ]]; then
  echo "missing data/bars — transfer Parquet first" >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  echo "missing .env (need CLICKHOUSE_PASSWORD)" >&2
  exit 1
fi

# shellcheck disable=SC1091
set -a
source .env
set +a

: "${CLICKHOUSE_PASSWORD:?set CLICKHOUSE_PASSWORD in .env}"
CLICKHOUSE_USER="${CLICKHOUSE_USER:-default}"

echo "waiting for ClickHouse..."
for _ in $(seq 1 60); do
  if docker compose exec -T clickhouse clickhouse-client \
    --user "$CLICKHOUSE_USER" \
    --password "$CLICKHOUSE_PASSWORD" \
    --query "SELECT 1" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

docker compose exec -T clickhouse clickhouse-client \
  --user "$CLICKHOUSE_USER" \
  --password "$CLICKHOUSE_PASSWORD" \
  --multiquery < infra/clickhouse/init/01_schema.sql

echo "loading Parquet from user_files/data/bars (this can take a while)..."
docker compose exec -T clickhouse clickhouse-client \
  --user "$CLICKHOUSE_USER" \
  --password "$CLICKHOUSE_PASSWORD" \
  --receive_timeout 3600 \
  --send_timeout 3600 \
  --query "
INSERT INTO backtester.bars
SELECT
    instrument_id,
    source,
    timeframe,
    ts_utc,
    open,
    high,
    low,
    close,
    volume,
    is_partial,
    quality,
    adj_close
FROM file('data/bars/**/*.parquet', Parquet)
SETTINGS
  input_format_parquet_allow_missing_columns = 1,
  max_partitions_per_insert_block = 10000
"

ROWS="$(docker compose exec -T clickhouse clickhouse-client \
  --user "$CLICKHOUSE_USER" \
  --password "$CLICKHOUSE_PASSWORD" \
  --query 'SELECT count() FROM backtester.bars')"

echo "loaded rows: ${ROWS}"
