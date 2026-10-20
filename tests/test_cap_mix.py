"""cap_mix: trailing-year leaders stay inside their size sleeve."""

from __future__ import annotations

from datetime import UTC, datetime

import polars as pl

from backtester.strategy.plugins.cap_mix import CapMix, CapMixParams, CapSleeve


def _panel() -> pl.DataFrame:
    days = [
        datetime(2022, 3, 31, 10, 0, tzinfo=UTC),
        datetime(2022, 6, 30, 10, 0, tzinfo=UTC),
        datetime(2022, 9, 30, 10, 0, tzinfo=UTC),
        datetime(2022, 12, 30, 10, 0, tzinfo=UTC),
        datetime(2023, 3, 31, 10, 0, tzinfo=UTC),
        datetime(2023, 4, 3, 10, 0, tzinfo=UTC),
    ]
    prices = {
        "EQ:XNSE:BIG": [100.0, 140.0, 180.0, 220.0, 300.0, 300.0],
        "EQ:XNSE:NEXT": [100.0, 120.0, 140.0, 160.0, 200.0, 200.0],
        "EQ:XNSE:THIRD": [100.0, 100.0, 100.0, 100.0, 110.0, 110.0],
        "EQ:XNSE:MID": [100.0, 150.0, 200.0, 250.0, 400.0, 400.0],
        "EQ:XNSE:SMALL": [100.0, 110.0, 120.0, 130.0, 180.0, 180.0],
    }
    rows = []
    for iid, series in prices.items():
        for ts, px in zip(days, series, strict=True):
            rows.append(
                {
                    "ts_utc": ts,
                    "instrument_id": iid,
                    "open": px,
                    "high": px,
                    "low": px,
                    "close": px,
                    "adj_close": px,
                }
            )
    return pl.DataFrame(rows).with_columns(pl.col("ts_utc").dt.convert_time_zone("UTC"))


def test_each_sleeve_keeps_its_own_leaders() -> None:
    params = CapMixParams(
        history_from="2000-01-01",
        trade_from="2001-01-01",
        min_prints=5,
        sleeves=(
            CapSleeve(n=2, ids=("EQ:XNSE:BIG", "EQ:XNSE:NEXT", "EQ:XNSE:THIRD")),
            CapSleeve(n=1, ids=("EQ:XNSE:MID",)),
            CapSleeve(n=1, ids=("EQ:XNSE:SMALL",)),
        ),
    )
    out = CapMix().targets(_panel(), params)
    quarter_end = datetime(2023, 3, 31, 10, 0, tzinfo=UTC)
    chosen = out.filter(pl.col("ts_utc") == quarter_end).sort("instrument_id")
    weights = dict(zip(chosen["instrument_id"].to_list(), chosen["weight"].to_list(), strict=True))
    assert weights == {
        "EQ:XNSE:BIG": 0.25,
        "EQ:XNSE:MID": 0.25,
        "EQ:XNSE:NEXT": 0.25,
        "EQ:XNSE:SMALL": 0.25,
        "EQ:XNSE:THIRD": 0.0,
    }
    april = out.filter(pl.col("ts_utc") == datetime(2023, 4, 3, 10, 0, tzinfo=UTC))
    assert april["weight"].to_list() == [None, None, None, None, None]
