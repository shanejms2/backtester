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


def test_half_move_steps_halfway_to_target() -> None:
    stamps = _stamps(4)
    panel = _panel(stamps, opens=[10.0] * 4, closes=[10.0] * 4)
    fills, _ = execute(
        panel,
        _targets(stamps, [1.0, 1.0, 1.0, 1.0]),
        BrokerConfig(band_floor=0.005, band_frac=0.20, half_move=True),
    )
    assert fills.height >= 1
    assert fills["ts_utc"][0] == stamps[1]
    assert float(fills["size"][0]) == pytest.approx(0.5 * 1.0 / 10.0)


def test_full_exit_when_target_is_zero() -> None:
    stamps = _stamps(4)
    panel = _panel(stamps, opens=[10.0] * 4, closes=[10.0] * 4)
    fills, _ = execute(
        panel,
        _targets(stamps, [1.0, 1.0, 0.0, 0.0]),
        BrokerConfig(band_floor=0.005, band_frac=0.20, half_move=True),
    )
    by_ts = {row["ts_utc"]: row for row in fills.to_dicts()}
    assert stamps[1] in by_ts
    assert stamps[3] in by_ts
    # Half-move path: 0 → 0.50 → 0.75, then target 0 exits the full 0.75 (not another half-step).
    assert float(by_ts[stamps[3]]["size"]) == pytest.approx(-0.075)


def test_truncate_last_bar_leaves_prior_fills() -> None:
    stamps = _stamps(6)
    panel = _panel(stamps, opens=[10.0] * 6, closes=[10.0, 11.0, 12.0, 11.0, 10.0, 11.0])
    weights = [1.0, 1.0, 0.0, 1.0, 0.5, 0.0]
    cfg = BrokerConfig(band_floor=0.005, band_frac=0.20, half_move=True)
    fills_full, _ = execute(panel, _targets(stamps, weights), cfg)
    last_ts = stamps[-1]
    truncated = panel.filter(pl.col("ts_utc") < last_ts)
    fills_cut, _ = execute(truncated, _targets(stamps[:-1], weights[:-1]), cfg)
    prior = fills_full.filter(pl.col("ts_utc") < last_ts).sort(["ts_utc", "instrument_id"])
    got = fills_cut.sort(["ts_utc", "instrument_id"])
    assert prior.equals(got)


def test_proportional_band_skips_small_delta() -> None:
    stamps = _stamps(4)
    panel = _panel(stamps, opens=[10.0] * 4, closes=[10.0] * 4)
    fills, _ = execute(
        panel,
        _targets(stamps, [1.0, 1.0, 0.85, 0.85]),
        BrokerConfig(band_floor=0.005, band_frac=0.20, half_move=False),
    )
    assert fills.height == 1
    assert fills["ts_utc"][0] == stamps[1]


def test_null_target_holds_last_weight() -> None:
    stamps = _stamps(4)
    panel = _panel(stamps, opens=[10.0] * 4, closes=[10.0] * 4)
    targets = pl.DataFrame(
        {
            "ts_utc": stamps,
            "instrument_id": [A] * 4,
            "weight": [1.0, None, None, None],
        }
    ).with_columns(pl.col("ts_utc").dt.convert_time_zone("UTC"))
    fills, equity = execute(panel, targets, BrokerConfig())
    assert fills.height == 1
    assert fills["ts_utc"][0] == stamps[1]
    assert float(equity["gross"][-1]) == pytest.approx(1.0)


def test_null_open_is_hold_not_wipeout() -> None:
    stamps = _stamps(4)
    panel = pl.DataFrame(
        {
            "ts_utc": stamps,
            "instrument_id": [A] * 4,
            "open": [10.0, 10.0, None, 12.0],
            "high": [10.5, 10.5, None, 12.5],
            "low": [9.5, 9.5, None, 11.5],
            "close": [10.0, 10.0, None, 12.0],
            "volume": [100.0] * 4,
            "fee_bps": [0.0] * 4,
        }
    ).with_columns(pl.col("ts_utc").dt.convert_time_zone("UTC"))
    targets = pl.DataFrame(
        {
            "ts_utc": stamps,
            "instrument_id": [A] * 4,
            "weight": [1.0, 1.0, 1.0, 1.0],
        }
    ).with_columns(pl.col("ts_utc").dt.convert_time_zone("UTC"))
    fills, equity = execute(panel, targets, BrokerConfig())
    assert stamps[2] not in fills["ts_utc"].to_list()
    eq1 = float(equity.filter(pl.col("ts_utc") == stamps[1])["equity"][0])
    eq2 = float(equity.filter(pl.col("ts_utc") == stamps[2])["equity"][0])
    assert eq2 == pytest.approx(eq1)
    assert float(equity["equity"][-1]) == pytest.approx(eq1 * 1.2)


def test_drift_lets_weights_follow_price_until_reset() -> None:
    stamps = _stamps(4)
    a_close = [100.0, 110.0, 121.0, 121.0]
    b_close = [100.0, 100.0, 100.0, 100.0]
    a_open = [100.0, 100.0, 110.0, 121.0]
    b_open = [100.0, 100.0, 100.0, 100.0]
    panel = pl.concat(
        [
            _panel(stamps, instrument_id=A, opens=a_open, closes=a_close),
            _panel(stamps, instrument_id=B, opens=b_open, closes=b_close),
        ]
    )
    # Signal on t0 fills t1 at 50/50. Null holds the drifted mix. Signal on t2 fills t3 back to 50/50.
    weight_a = [0.5, None, 0.5, None]
    weight_b = [0.5, None, 0.5, None]
    targets = pl.concat(
        [
            _targets(stamps, weight_a, instrument_id=A),
            _targets(stamps, weight_b, instrument_id=B),
        ]
    )
    _, drifted = execute(panel, targets, BrokerConfig(drift=True))
    # Day t1: 50/50 and A +10% → 1.05. Day t2: drifted A is 0.55/1.05, another +10%.
    assert float(drifted.filter(pl.col("ts_utc") == stamps[1])["equity"][0]) == pytest.approx(1.05)
    assert float(drifted.filter(pl.col("ts_utc") == stamps[2])["equity"][0]) == pytest.approx(
        1.05 * (1.0 + 0.55 / 1.05 * 0.1)
    )
    constant = pl.concat(
        [
            _targets(stamps, [0.5, 0.5, 0.5, 0.5], instrument_id=A),
            _targets(stamps, [0.5, 0.5, 0.5, 0.5], instrument_id=B),
        ]
    )
    _, mixed = execute(panel, constant, BrokerConfig(drift=False))
    assert float(mixed.filter(pl.col("ts_utc") == stamps[2])["equity"][0]) == pytest.approx(1.05 * 1.05)
    assert float(drifted.filter(pl.col("ts_utc") == stamps[2])["equity"][0]) > float(
        mixed.filter(pl.col("ts_utc") == stamps[2])["equity"][0]
    )
    fills, _ = execute(panel, targets, BrokerConfig(drift=True))
    reset = fills.filter(pl.col("ts_utc") == stamps[3])
    assert reset.height == 2
    assert set(reset["instrument_id"].to_list()) == {A, B}


def test_cash_overlay_on_flat_book() -> None:
    stamps = _stamps(3)
    panel = _panel(stamps, opens=[10.0] * 3, closes=[10.0] * 3).with_columns(pl.lit(0.01).alias("daily_tbill"))
    fills, equity = execute(panel, _targets(stamps, [0.0, 0.0, 0.0]), BrokerConfig())
    assert fills.height == 0
    assert float(equity["equity"][-1]) == pytest.approx(1.01**3)
    assert float(equity["cash_weight"][-1]) == pytest.approx(1.0)
