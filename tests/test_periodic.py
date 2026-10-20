"""periodic: quarterly reset Targets. No live APIs."""

from __future__ import annotations

from datetime import UTC, datetime

import polars as pl

from backtester.strategy.plugins.periodic import Periodic, PeriodicParams, rebalance_stamps


def test_rebalance_stamps_are_first_bar_and_quarter_ends() -> None:
    stamps = [
        datetime(2024, 3, 28, 20, 0, tzinfo=UTC),
        datetime(2024, 3, 29, 20, 0, tzinfo=UTC),
        datetime(2024, 4, 1, 20, 0, tzinfo=UTC),
        datetime(2024, 4, 2, 20, 0, tzinfo=UTC),
    ]
    chosen = rebalance_stamps(stamps)
    assert stamps[0] in chosen
    assert stamps[1] in chosen
    assert stamps[2] not in chosen
    assert stamps[3] not in chosen


def test_periodic_emits_weights_only_on_reset() -> None:
    stamps = [
        datetime(2024, 3, 28, 20, 0, tzinfo=UTC),
        datetime(2024, 3, 29, 20, 0, tzinfo=UTC),
        datetime(2024, 4, 1, 20, 0, tzinfo=UTC),
    ]
    rows = []
    for ts in stamps:
        for iid in ("EQ:XNAS:QQQ", "EQ:ARCX:GLD"):
            rows.append({"ts_utc": ts, "instrument_id": iid})
    panel = pl.DataFrame(rows).with_columns(pl.col("ts_utc").dt.convert_time_zone("UTC"))
    out = Periodic().targets(
        panel,
        PeriodicParams(weights={"EQ:XNAS:QQQ": 0.5, "EQ:ARCX:GLD": 0.5}),
    )
    april = out.filter(pl.col("ts_utc") == stamps[2])
    assert april["weight"].to_list() == [None, None]
    march_end = out.filter(pl.col("ts_utc") == stamps[1]).sort("instrument_id")
    assert march_end["weight"].to_list() == [0.5, 0.5]
