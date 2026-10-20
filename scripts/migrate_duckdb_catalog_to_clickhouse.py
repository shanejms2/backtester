#!/usr/bin/env python3
"""One-shot: copy DuckDB catalog (+ optional snapshot Parquet) into ClickHouse.

Bars should already be loaded (scripts/load_clickhouse_bars.sh). Requires:
  pip install -e ".[migrate]"
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import clickhouse_connect
import duckdb
import polars as pl
from dotenv import load_dotenv


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path("data/catalog.duckdb"),
        help="Path to catalog.duckdb",
    )
    parser.add_argument(
        "--snapshots",
        type=Path,
        default=Path("data/snapshots"),
        help="Optional Hive snapshots directory",
    )
    args = parser.parse_args()
    load_dotenv(Path.cwd() / ".env", override=False)

    if not args.catalog.is_file():
        print(f"missing catalog: {args.catalog}", file=sys.stderr)
        return 1

    host = os.environ.get("CLICKHOUSE_HOST", "127.0.0.1")
    port = int(os.environ.get("CLICKHOUSE_PORT", "8123"))
    user = os.environ.get("CLICKHOUSE_USER", "default")
    password = os.environ.get("CLICKHOUSE_PASSWORD", "")
    database = os.environ.get("CLICKHOUSE_DATABASE", "backtester")

    from backtester.store.clickhouse import ClickHouseStore

    store = ClickHouseStore(
        host=host,
        port=port,
        username=user,
        password=password,
        database=database,
    )
    client = clickhouse_connect.get_client(
        host=host, port=port, username=user, password=password, database=database
    )
    duck = duckdb.connect(str(args.catalog), read_only=True)

    def _table(name: str) -> pl.DataFrame:
        result = duck.execute(f"SELECT * FROM {name}")
        cols = [d[0] for d in result.description]
        rows = result.fetchall()
        if not rows:
            return pl.DataFrame(schema={c: pl.Utf8 for c in cols})
        return pl.DataFrame(rows, schema=cols, orient="row")

    instruments = _table("instruments")
    if instruments.height:
        client.command(f"TRUNCATE TABLE IF EXISTS {database}.instruments")
        client.command(f"TRUNCATE TABLE IF EXISTS {database}.aliases")
        store._insert_frame(f"{database}.instruments", instruments, instruments.columns)  # noqa: SLF001
        print(f"instruments: {instruments.height}")

    aliases = _table("aliases")
    if aliases.height:
        store._insert_frame(f"{database}.aliases", aliases, aliases.columns)  # noqa: SLF001
        print(f"aliases: {aliases.height}")

    coverage = _table("coverage")
    if coverage.height:
        client.command(f"TRUNCATE TABLE IF EXISTS {database}.coverage")
        store._insert_frame(f"{database}.coverage", coverage, coverage.columns)  # noqa: SLF001
        print(f"coverage: {coverage.height}")

    tables = {r[0] for r in duck.execute("SHOW TABLES").fetchall()}
    if "snapshot_polls" in tables:
        polls = _table("snapshot_polls")
        if polls.height:
            client.command(f"TRUNCATE TABLE IF EXISTS {database}.snapshot_polls")
            store._insert_frame(f"{database}.snapshot_polls", polls, polls.columns)  # noqa: SLF001
            print(f"snapshot_polls: {polls.height}")

    duck.close()

    if args.snapshots.is_dir():
        from backtester.store.clickhouse import SNAPSHOT_COLUMNS

        paths = list(args.snapshots.rglob("*.parquet"))
        if paths:
            client.command(f"TRUNCATE TABLE IF EXISTS {database}.snapshots")
            frame = pl.concat([pl.read_parquet(p) for p in paths], how="diagonal_relaxed")
            keep = [c for c in SNAPSHOT_COLUMNS if c in frame.columns]
            result = store.write_snapshots(frame.select(keep))
            print(f"snapshots: {result.rows_written} from {len(paths)} files")

    bars = client.query(f"SELECT count() FROM {database}.bars").result_rows[0][0]
    print(f"bars already in ClickHouse: {bars}")
    store.close()
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
