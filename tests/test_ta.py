"""Causal Polars TA: hand values, group isolation, no look-ahead."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from backtester import ta


def _panel(
    closes: list[float],
    *,
    instrument_id: str = "EQ:XNAS:AAPL",
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    opens: list[float] | None = None,
    volumes: list[float] | None = None,
    start: datetime | None = None,
) -> pl.DataFrame:
    n = len(closes)
    origin = start or datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    highs = highs if highs is not None else [c + 1.0 for c in closes]
    lows = lows if lows is not None else [c - 1.0 for c in closes]
    opens = opens if opens is not None else list(closes)
    volumes = volumes if volumes is not None else [100.0] * n
    return pl.DataFrame(
        {
            "instrument_id": [instrument_id] * n,
            "ts_utc": [origin + timedelta(days=i) for i in range(n)],
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        }
    ).with_columns(pl.col("ts_utc").dt.convert_time_zone("UTC"))


def _approx_list(got: list[object], expected: list[object], rel: float = 1e-9) -> None:
    assert len(got) == len(expected)
    for a, b in zip(got, expected, strict=True):
        if a is None and b is None:
            continue
        assert a == pytest.approx(b, rel=rel, abs=1e-12)


def test_available_matches_public_catalog() -> None:
    names = ta.available()
    assert names == list(ta.CATALOG)
    for name in names:
        assert callable(getattr(ta, name))


def test_sma_hand_values_and_warmup() -> None:
    panel = _panel([1.0, 2.0, 3.0, 4.0, 5.0])
    got = ta.sma(panel, length=3).to_list()
    _approx_list(got, [None, None, 2.0, 3.0, 4.0])


def test_rsi_wilder_hand_values_and_warmup() -> None:
    # Changes: +2, -1, +2. First RSI uses the first two changes (Wilder SMA seed).
    panel = _panel([10.0, 12.0, 11.0, 13.0])
    got = ta.rsi(panel, length=2).to_list()
    assert got[0] is None and got[1] is None
    # avg_gain=1, avg_loss=0.5 → RSI = 100 - 100/3
    assert got[2] == pytest.approx(100.0 - 100.0 / 3.0)
    # next: avg_gain=1.5, avg_loss=0.25 → RSI = 100 - 100/7
    assert got[3] == pytest.approx(100.0 - 100.0 / 7.0)


def test_atr_hand_values_and_warmup() -> None:
    panel = _panel(
        closes=[9.0, 10.0, 8.0],
        highs=[10.0, 11.0, 12.0],
        lows=[8.0, 9.0, 7.0],
        opens=[9.0, 10.0, 10.0],
    )
    got = ta.atr(panel, length=2).to_list()
    # TR: 2, 2, 5. Seed ATR = 2; next Wilder (2 + 5) / 2 = 3.5
    _approx_list(got, [None, 2.0, 3.5])


def test_ema_sma_seed() -> None:
    panel = _panel([1.0, 2.0, 3.0, 4.0])
    got = ta.ema(panel, length=3).to_list()
    # alpha = 2/4 = 0.5; seed SMA = 2; next = 0.5*4 + 0.5*2 = 3
    _approx_list(got, [None, None, 2.0, 3.0])


def test_two_name_rsi_does_not_leak() -> None:
    a = _panel([10.0, 11.0, 10.5, 12.0, 11.5, 13.0], instrument_id="A")
    b = _panel([1.0, 2.0, 3.0, 4.0, 5.0, 6.0], instrument_id="B")
    panel = pl.concat([a, b])
    rsi_a = (
        ta.rsi(panel, length=3)
        .to_frame("rsi")
        .hstack(panel.select("instrument_id"))
        .filter(pl.col("instrument_id") == "A")["rsi"]
    )
    scrambled = pl.concat(
        [
            a,
            b.with_columns(pl.col("close").reverse()),
        ]
    )
    rsi_a_scrambled = (
        ta.rsi(scrambled, length=3)
        .to_frame("rsi")
        .hstack(scrambled.select("instrument_id"))
        .filter(pl.col("instrument_id") == "A")["rsi"]
    )
    _approx_list(rsi_a.to_list(), rsi_a_scrambled.to_list())


def test_truncating_last_bar_does_not_change_previous_values() -> None:
    panel = _panel([10.0, 11.0, 10.5, 12.0, 11.2, 13.0, 12.5, 14.0])
    truncated = panel.slice(0, panel.height - 1)

    def _check(full: pl.Series, trunc: pl.Series) -> None:
        _approx_list(full.to_list()[:-1], trunc.to_list())

    _check(ta.sma(panel, length=3), ta.sma(truncated, length=3))
    _check(ta.rsi(panel, length=3), ta.rsi(truncated, length=3))
    _check(ta.atr(panel, length=3), ta.atr(truncated, length=3))
    _check(ta.ema(panel, length=3), ta.ema(truncated, length=3))
    _check(ta.swing_high(panel, left=1, right=1), ta.swing_high(truncated, left=1, right=1))


def test_wma_hand_values() -> None:
    panel = _panel([1.0, 2.0, 3.0])
    got = ta.wma(panel, length=3).to_list()
    _approx_list(got, [None, None, (1 * 1 + 2 * 2 + 3 * 3) / 6])


def test_vwap_resets_on_session_date() -> None:
    origin = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    panel = _panel(
        [10.0, 12.0, 14.0],
        highs=[10.0, 12.0, 14.0],
        lows=[10.0, 12.0, 14.0],
        volumes=[1.0, 1.0, 1.0],
        start=origin,
    ).with_columns(
        pl.Series(
            "session_date", [datetime(2024, 1, 2).date(), datetime(2024, 1, 2).date(), datetime(2024, 1, 3).date()]
        )
    )
    got = ta.vwap(panel).to_list()
    # typical price = close here; day1: 10 then (10+12)/2 = 11; day2 resets to 14
    _approx_list(got, [10.0, 11.0, 14.0])


def test_vwap_utc_date_when_no_session_column() -> None:
    origin = datetime(2024, 1, 2, 23, 0, tzinfo=UTC)
    panel = _panel(
        [10.0, 20.0],
        highs=[10.0, 20.0],
        lows=[10.0, 20.0],
        volumes=[1.0, 1.0],
        start=origin,
    )
    # second bar is next UTC date → reset
    got = ta.vwap(panel).to_list()
    _approx_list(got, [10.0, 20.0])


def test_swing_high_confirms_on_right_bar() -> None:
    # Pivot high at index 1 (15) with left=1,right=1: confirm at index 2
    panel = _panel(
        [10.0, 12.0, 11.0, 13.0],
        highs=[10.0, 15.0, 11.0, 13.0],
        lows=[9.0, 10.0, 10.0, 12.0],
    )
    flags = ta.swing_high(panel, left=1, right=1).to_list()
    assert flags[0] is None
    assert flags[1] is None  # pivot at 0 has no left bar
    assert flags[2] is True
    assert flags[3] is False
    labels = ta.swing_labels(panel, left=1, right=1).to_list()
    assert labels[2] == "H"


def test_catalog_functions_run_on_fixture_panel() -> None:
    closes = [float(10 + (i % 7) - 3) for i in range(40)]
    panel = _panel(closes)
    two = pl.concat([panel, _panel(list(reversed(closes)), instrument_id="B")])
    series_fns = ("sma", "ema", "wma", "rsi", "cci", "willr", "roc", "mom", "atr", "stdev", "obv", "mfi", "cmf", "vwap")
    for name in series_fns:
        out = getattr(ta, name)(two)
        assert isinstance(out, pl.Series)
        assert out.len() == two.height
    frames = {
        "macd": ta.macd(two),
        "adx": ta.adx(two),
        "supertrend": ta.supertrend(two),
        "aroon": ta.aroon(two),
        "stoch": ta.stoch(two),
        "stoch_rsi": ta.stoch_rsi(two),
        "bbands": ta.bbands(two),
        "keltner": ta.keltner(two),
        "donchian": ta.donchian(two),
        "swings": ta.swings(two, left=2, right=2),
    }
    for name, frame in frames.items():
        assert frame.height == two.height, name


def test_missing_column_raises() -> None:
    panel = pl.DataFrame({"instrument_id": ["A"], "ts_utc": [datetime(2024, 1, 1, tzinfo=UTC)]})
    with pytest.raises(ValueError, match="missing required columns"):
        ta.sma(panel, length=2)
