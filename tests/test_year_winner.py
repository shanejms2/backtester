"""year_winner: full prior calendar year, filled the next open by the Broker."""

from __future__ import annotations

from datetime import UTC, datetime

import polars as pl

from backtester.strategy.plugins.year_winner import YearWinner, YearWinnerParams


def _panel() -> pl.DataFrame:
    # 10:00 UTC is 15:30 IST, so the session date matches the stamp date.
    days = [
        datetime(2023, 1, 3, 10, 0, tzinfo=UTC),
        datetime(2023, 6, 1, 10, 0, tzinfo=UTC),
        datetime(2023, 12, 29, 10, 0, tzinfo=UTC),
        datetime(2024, 1, 2, 10, 0, tzinfo=UTC),
    ]
    rows = []
    prices = {
        "EQ:XNSE:SLOW": [100.0, 105.0, 110.0, 110.0],
        "EQ:XNSE:FAST": [100.0, 130.0, 160.0, 160.0],
        "EQ:XNSE:LATE": [None, 10.0, 50.0, 50.0],
    }
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


def test_year_end_picks_the_best_full_year() -> None:
    params = YearWinnerParams(history_from="2000-01-01", trade_from="2001-01-01", min_prints=2)
    out = YearWinner().targets(_panel(), params)
    year_end = datetime(2023, 12, 29, 10, 0, tzinfo=UTC)
    chosen = out.filter(pl.col("ts_utc") == year_end).sort("instrument_id")
    weights = dict(zip(chosen["instrument_id"].to_list(), chosen["weight"].to_list(), strict=True))
    assert weights == {"EQ:XNSE:FAST": 1.0, "EQ:XNSE:LATE": 0.0, "EQ:XNSE:SLOW": 0.0}
    june = out.filter(pl.col("ts_utc") == datetime(2023, 6, 1, 10, 0, tzinfo=UTC))
    assert june["weight"].to_list() == [None, None, None]


def test_top_two_share_the_book_equally() -> None:
    params = YearWinnerParams(history_from="2000-01-01", trade_from="2001-01-01", min_prints=2, n=2)
    out = YearWinner().targets(_panel(), params)
    year_end = datetime(2023, 12, 29, 10, 0, tzinfo=UTC)
    chosen = out.filter(pl.col("ts_utc") == year_end).sort("instrument_id")
    weights = dict(zip(chosen["instrument_id"].to_list(), chosen["weight"].to_list(), strict=True))
    assert weights == {"EQ:XNSE:FAST": 0.5, "EQ:XNSE:LATE": 0.0, "EQ:XNSE:SLOW": 0.5}
    wider = YearWinnerParams(history_from="2000-01-01", trade_from="2001-01-01", min_prints=2, n=3)
    out3 = YearWinner().targets(_panel(), wider)
    chosen3 = out3.filter(pl.col("ts_utc") == year_end).sort("instrument_id")
    weights3 = dict(zip(chosen3["instrument_id"].to_list(), chosen3["weight"].to_list(), strict=True))
    assert weights3 == {"EQ:XNSE:FAST": 0.5, "EQ:XNSE:LATE": 0.0, "EQ:XNSE:SLOW": 0.5}


def _quarter_panel() -> pl.DataFrame:
    days = [
        datetime(2022, 3, 31, 10, 0, tzinfo=UTC),
        datetime(2022, 6, 30, 10, 0, tzinfo=UTC),
        datetime(2022, 9, 30, 10, 0, tzinfo=UTC),
        datetime(2022, 12, 30, 10, 0, tzinfo=UTC),
        datetime(2023, 3, 31, 10, 0, tzinfo=UTC),
        datetime(2023, 4, 3, 10, 0, tzinfo=UTC),
    ]
    prices = {
        "EQ:XNSE:FAST": [100.0, 120.0, 140.0, 160.0, 160.0, 160.0],
        "EQ:XNSE:MID": [100.0, 110.0, 130.0, 150.0, 180.0, 180.0],
        "EQ:XNSE:SPIKE": [100.0, 80.0, 60.0, 50.0, 110.0, 110.0],
        "EQ:XNSE:LATE": [None, 10.0, 20.0, 30.0, 500.0, 500.0],
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


def test_quarter_end_ranks_the_trailing_year() -> None:
    params = YearWinnerParams(history_from="2000-01-01", trade_from="2001-01-01", min_prints=5, n=2, every="quarter")
    out = YearWinner().targets(_quarter_panel(), params)
    quarter_end = datetime(2023, 3, 31, 10, 0, tzinfo=UTC)
    chosen = out.filter(pl.col("ts_utc") == quarter_end).sort("instrument_id")
    weights = dict(zip(chosen["instrument_id"].to_list(), chosen["weight"].to_list(), strict=True))
    assert weights == {
        "EQ:XNSE:FAST": 0.5,
        "EQ:XNSE:LATE": 0.0,
        "EQ:XNSE:MID": 0.5,
        "EQ:XNSE:SPIKE": 0.0,
    }
    too_early = out.filter(pl.col("ts_utc") == datetime(2022, 6, 30, 10, 0, tzinfo=UTC))
    assert too_early["weight"].to_list() == [None, None, None, None]


def test_stop_sells_under_the_average_until_the_next_reset() -> None:
    days = [
        datetime(2023, 1, 3, 10, 0, tzinfo=UTC),
        datetime(2023, 12, 29, 10, 0, tzinfo=UTC),
        datetime(2024, 1, 2, 10, 0, tzinfo=UTC),
        datetime(2024, 1, 3, 10, 0, tzinfo=UTC),
        datetime(2024, 1, 4, 10, 0, tzinfo=UTC),
    ]
    prices = [10.0, 20.0, 30.0, 10.0, 40.0]
    rows = [
        {
            "ts_utc": ts,
            "instrument_id": "EQ:XNSE:FAST",
            "open": px,
            "high": px,
            "low": px,
            "close": px,
            "adj_close": px,
        }
        for ts, px in zip(days, prices, strict=True)
    ]
    panel = pl.DataFrame(rows).with_columns(pl.col("ts_utc").dt.convert_time_zone("UTC"))
    params = YearWinnerParams(history_from="2000-01-01", trade_from="2001-01-01", min_prints=2, stop_bars=2)
    out = YearWinner().targets(panel, params)
    weights = out["weight"].to_list()
    assert weights[1] == 1.0
    assert weights[2] is None
    assert weights[3] == 0.0
    assert weights[4] is None


def test_ready_bars_skips_the_history_year() -> None:
    params = YearWinnerParams(history_from="2000-01-01", trade_from="2001-01-01")
    assert 240 <= YearWinner().ready_bars(params) <= 260
