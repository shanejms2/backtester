"""Broker: next-bar open, signed weights, class fees, rebalance band."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from backtester.strategy.broker import execute
from backtester.strategy.types import BrokerConfig

A = "EQ:XNAS:AAPL"
B = "CRYPTO:BINANCE:BTC/USDT"


def _stamps(n: int, start: datetime | None = None) -> list[datetime]:
    origin = start or datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    return [origin + timedelta(days=i) for i in range(n)]


def _panel(
    stamps: list[datetime],
    *,
    instrument_id: str = A,
    opens: list[float] | None = None,
    closes: list[float] | None = None,
    fee_bps: float = 0.0,
) -> pl.DataFrame:
    n = len(stamps)
    closes = closes if closes is not None else [10.0 + i for i in range(n)]
    opens = opens if opens is not None else list(closes)
    highs = [max(o, c) + 0.5 for o, c in zip(opens, closes, strict=True)]
    lows = [min(o, c) - 0.5 for o, c in zip(opens, closes, strict=True)]
    return pl.DataFrame(
        {
            "ts_utc": stamps,
            "instrument_id": [instrument_id] * n,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [100.0] * n,
            "fee_bps": [fee_bps] * n,
        }
    ).with_columns(pl.col("ts_utc").dt.convert_time_zone("UTC"))


def _targets(stamps: list[datetime], weights: list[float], *, instrument_id: str = A) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts_utc": stamps,
            "instrument_id": [instrument_id] * len(stamps),
            "weight": weights,
        }
    ).with_columns(pl.col("ts_utc").dt.convert_time_zone("UTC"))


def test_target_at_t_does_not_change_fill_at_open_t() -> None:
    stamps = _stamps(5)
    panel = _panel(stamps, opens=[10.0] * 5, closes=[10.0, 11.0, 12.0, 11.0, 10.0])
    base = _targets(stamps, [1.0, 1.0, 1.0, 1.0, 1.0])
    fills_a, _ = execute(panel, base, BrokerConfig())
    changed_last = _targets(stamps, [1.0, 1.0, 1.0, 1.0, 0.0])
    fills_b, _ = execute(panel, changed_last, BrokerConfig())
    assert fills_a.equals(fills_b)

    changed_mid = _targets(stamps, [1.0, 1.0, 0.0, 1.0, 1.0])
    fills_c, _ = execute(panel, changed_mid, BrokerConfig())
    t2 = stamps[2]
    early_a = fills_a.filter(pl.col("ts_utc") <= t2)
    early_c = fills_c.filter(pl.col("ts_utc") <= t2)
    assert early_a.equals(early_c)
    later_c = fills_c.filter(pl.col("ts_utc") > t2)
    assert later_c.height > 0


def test_long_short_and_flip_on_three_bars() -> None:
    stamps = _stamps(3)
    panel = _panel(stamps, opens=[10.0, 10.0, 12.0], closes=[10.0, 12.0, 9.0])
    cfg = BrokerConfig(rebalance_band=0.0, allow_short=True)

    long_fills, _ = execute(panel, _targets(stamps, [1.0, 1.0, 1.0]), cfg)
    assert long_fills.height == 1
    assert long_fills["ts_utc"][0] == stamps[1]
    assert float(long_fills["size"][0]) > 0

    short_fills, _ = execute(panel, _targets(stamps, [-1.0, -1.0, -1.0]), cfg)
    assert short_fills.height == 1
    assert short_fills["ts_utc"][0] == stamps[1]
    assert float(short_fills["size"][0]) < 0

    flip_fills, _ = execute(panel, _targets(stamps, [1.0, -1.0, -1.0]), cfg)
    assert flip_fills.height == 2
    assert flip_fills["ts_utc"].to_list() == [stamps[1], stamps[2]]
    assert float(flip_fills["size"][0]) > 0
    assert float(flip_fills["size"][1]) < 0


def test_allow_short_false_zeros_negative_targets() -> None:
    stamps = _stamps(3)
    panel = _panel(stamps, opens=[10.0] * 3, closes=[10.0] * 3)
    fills, equity = execute(
        panel,
        _targets(stamps, [-1.0, -1.0, -1.0]),
        BrokerConfig(allow_short=False),
    )
    assert fills.height == 0
    assert float(equity["equity"][-1]) == pytest.approx(1.0)


def test_small_target_delta_below_band_is_zero_fill() -> None:
    stamps = _stamps(4)
    panel = _panel(stamps, opens=[10.0] * 4, closes=[10.0] * 4, fee_bps=10.0)
    targets = _targets(stamps, [0.50, 0.52, 0.53, 0.54])
    fills, _ = execute(panel, targets, BrokerConfig(rebalance_band=0.05))
    assert fills.height == 1
    assert fills["ts_utc"][0] == stamps[1]
    assert float(fills["size"][0]) == pytest.approx(0.5 * 1.0 / 10.0)


def test_class_fees_charged_on_traded_notional() -> None:
    stamps = _stamps(3)
    panel_a = _panel(stamps, instrument_id=A, opens=[10.0] * 3, closes=[10.0] * 3, fee_bps=0.0)
    panel_b = _panel(stamps, instrument_id=B, opens=[10.0] * 3, closes=[10.0] * 3, fee_bps=1000.0)
    panel = pl.concat([panel_a, panel_b])
    targets = pl.concat(
        [
            _targets(stamps, [0.5, 0.5, 0.5], instrument_id=A),
            _targets(stamps, [0.5, 0.5, 0.5], instrument_id=B),
        ]
    )
    fills, equity = execute(panel, targets, BrokerConfig())
    by_id = {row["instrument_id"]: row for row in fills.to_dicts()}
    assert by_id[A]["fee"] == pytest.approx(0.0)
    assert by_id[B]["fee"] == pytest.approx(0.5 * 1.0 * 1000.0 / 10_000.0)
    assert float(equity.filter(pl.col("ts_utc") == stamps[1])["equity"][0]) == pytest.approx(0.95)
