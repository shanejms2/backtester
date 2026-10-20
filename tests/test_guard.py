"""guard: slow-average exit, fast-average re-entry, cash in the gap."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest
from pydantic import ValidationError

from backtester.strategy.engine import run_spec
from backtester.strategy.plugins.guard import Guard, GuardParams
from backtester.strategy.spec import load_spec
from tests.conftest import aapl, btc


def _panel(closes: list[float]) -> pl.DataFrame:
    origin = datetime(2024, 1, 2, 20, 0, tzinfo=UTC)
    stamps = [origin + timedelta(days=i) for i in range(len(closes))]
    rows = []
    for ts, close in zip(stamps, closes, strict=True):
        for iid, level in (("EQ:XNAS:QQQ", close), ("EQ:ARCX:GLD", 20.0)):
            rows.append(
                {
                    "ts_utc": ts,
                    "instrument_id": iid,
                    "open": level,
                    "high": level,
                    "low": level,
                    "close": level,
                    "adj_close": level,
                }
            )
    return pl.DataFrame(rows).with_columns(pl.col("ts_utc").dt.convert_time_zone("UTC"))


def test_exit_below_slow_average_and_reenter_above_fast() -> None:
    # exit 3, enter 2. Day 3 breaks the slow average. Day 4 is still under it
    # and already back above the fast average, so the book re-enters.
    panel = _panel([10.0, 10.0, 10.0, 7.0, 8.0, 12.0])
    params = GuardParams(
        guard="EQ:XNAS:QQQ",
        exit_bars=3,
        enter_bars=2,
        risk_on={"EQ:XNAS:QQQ": 0.7, "EQ:ARCX:GLD": 0.3},
        risk_off={"EQ:ARCX:GLD": 0.5},
    )
    out = Guard().targets(panel, params)
    qqq = out.filter(pl.col("instrument_id") == "EQ:XNAS:QQQ").sort("ts_utc")["weight"].to_list()
    gld = out.filter(pl.col("instrument_id") == "EQ:ARCX:GLD").sort("ts_utc")["weight"].to_list()
    assert qqq == [0.7, 0.7, 0.7, 0.0, 0.7, 0.7]
    assert gld == [0.3, 0.3, 0.3, 0.5, 0.3, 0.3]
    assert Guard().ready_bars(params) == 0


def test_null_guard_price_holds_the_prior_book() -> None:
    panel = _panel([10.0, 10.0, 10.0, 7.0])
    panel = panel.with_columns(
        pl.when((pl.col("instrument_id") == "EQ:XNAS:QQQ") & (pl.col("close") == 7.0))
        .then(None)
        .otherwise(pl.col("close"))
        .alias("close"),
    ).with_columns(pl.col("close").alias("adj_close"))
    params = GuardParams(
        guard="EQ:XNAS:QQQ",
        exit_bars=3,
        enter_bars=2,
        risk_on={"EQ:XNAS:QQQ": 0.7, "EQ:ARCX:GLD": 0.3},
        risk_off={"EQ:ARCX:GLD": 0.5},
    )
    qqq = Guard().targets(panel, params).filter(pl.col("instrument_id") == "EQ:XNAS:QQQ")["weight"].to_list()
    assert qqq[-1] == 0.7


def test_reentry_waits_until_the_slow_average_is_rising() -> None:
    # Day 4 is back above the fast average while the slow average is still
    # falling, so rise_bars keeps the book off. Day 6 clears both.
    panel = _panel([10.0, 10.0, 10.0, 7.0, 9.0, 9.0, 10.0])
    params = GuardParams(
        guard="EQ:XNAS:QQQ",
        exit_bars=3,
        enter_bars=2,
        rise_bars=1,
        risk_on={"EQ:XNAS:QQQ": 0.7, "EQ:ARCX:GLD": 0.3},
        risk_off={"EQ:ARCX:GLD": 0.5},
    )
    qqq = (
        Guard()
        .targets(panel, params)
        .filter(pl.col("instrument_id") == "EQ:XNAS:QQQ")
        .sort("ts_utc")["weight"]
        .to_list()
    )
    assert qqq == [0.7, 0.7, 0.7, 0.0, 0.0, 0.0, 0.7]


def test_guard_params_reject_a_fast_window_that_is_not_faster() -> None:
    with pytest.raises(ValidationError, match="enter_bars"):
        GuardParams(guard="EQ:XNAS:QQQ", exit_bars=50, enter_bars=50, risk_on={"EQ:XNAS:QQQ": 1.0})


def test_run_drift_overrides_the_spec(tmp_store, tmp_path: Path) -> None:  # noqa: ANN001
    eq, crypto = aapl(), btc()
    origin = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    stamps = [origin + timedelta(days=i) for i in range(4)]
    a_close = [100.0, 110.0, 121.0, 121.0]
    b_close = [100.0, 100.0, 100.0, 100.0]
    for inst, closes in ((eq, a_close), (crypto, b_close)):
        opens = [closes[0], *closes[:-1]]
        tmp_store.upsert_instrument(inst)
        tmp_store.write_batch(
            pl.DataFrame(
                {
                    "instrument_id": [inst.instrument_id] * 4,
                    "source": ["yahoo"] * 4,
                    "timeframe": ["1d"] * 4,
                    "ts_utc": stamps,
                    "open": opens,
                    "high": [c + 0.5 for c in closes],
                    "low": [c - 0.5 for c in closes],
                    "close": closes,
                    "volume": [100.0] * 4,
                    "is_partial": [False] * 4,
                    "quality": ["ok"] * 4,
                }
            ).with_columns(pl.col("ts_utc").dt.convert_time_zone("UTC"))
        )
        tmp_store.update_coverage(inst.instrument_id, "1d", "yahoo", stamps[0], stamps[-1])
    path = tmp_path / "drift.yaml"
    path.write_text(
        f"""name: drift-override
timeframe: 1d
from: 2024-01-01
to: 2024-01-10
source: yahoo
drift: true
universe:
  - id: {eq.instrument_id}
    class: equity
    calendar: native_24_7
    fee_bps: 0
  - id: {crypto.instrument_id}
    class: crypto
    calendar: native_24_7
    fee_bps: 0
runs:
  - id: inherit
    strategy: periodic
    params:
      every: quarter
      weights:
        "{eq.instrument_id}": 0.5
        "{crypto.instrument_id}": 0.5
  - id: pinned
    strategy: periodic
    drift: false
    params:
      every: quarter
      weights:
        "{eq.instrument_id}": 0.5
        "{crypto.instrument_id}": 0.5
""",
        encoding="utf-8",
    )
    results = {item.run_id: item for item in run_spec(load_spec(path), tmp_store)}
    inherited = float(results["inherit"].equity["equity"][-1])
    pinned = float(results["pinned"].equity["equity"][-1])
    assert inherited == pytest.approx(1.05 * (1.0 + 0.55 / 1.05 * 0.1))
    assert pinned == pytest.approx(1.05 * 1.05)
    assert inherited > pinned
