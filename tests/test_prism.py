"""PRISM Strategy: clusters, liquidity, hysteresis. Fixtures only — no live APIs."""

from __future__ import annotations

import inspect
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from backtester.strategy.broker import execute
from backtester.strategy.plugins.prism import (
    Prism,
    PrismParams,
    _role_haircut,
    apply_hysteresis,
    cluster_labels,
    ledoit_wolf,
)
from backtester.strategy.types import BrokerConfig

DUP = "EQ:ARCX:DUP"
COPY = "EQ:ARCX:COPY"
SYN = "EQ:ARCX:SYN"
TINY = "EQ:ARCX:TINY"
DEAD = "EQ:ARCX:DEAD"


def _weekdays(n: int, start: datetime | None = None) -> list[datetime]:
    origin = start or datetime(2020, 1, 6, 21, 0, tzinfo=UTC)
    out: list[datetime] = []
    day = origin
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def _uptrend(n: int, seed: int, *, mu: float = 0.0015, sigma: float = 0.008) -> list[float]:
    rng = random.Random(seed)
    px = 100.0
    out: list[float] = []
    for _ in range(n):
        px *= 1.0 + mu + sigma * rng.gauss(0.0, 1.0)
        out.append(max(px, 0.01))
    return out


def _downtrend(n: int) -> list[float]:
    return [max(100.0 - i * 0.05, 1.0) for i in range(n)]


def _name_frame(
    instrument_id: str,
    stamps: list[datetime],
    closes: list[float],
    *,
    volume: float,
    role: str,
) -> pl.DataFrame:
    n = len(stamps)
    return pl.DataFrame(
        {
            "ts_utc": stamps,
            "instrument_id": [instrument_id] * n,
            "open": closes,
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "adj_close": closes,
            "volume": [volume] * n,
            "role": [role] * n,
        }
    ).with_columns(pl.col("ts_utc").dt.convert_time_zone("UTC"))


def _fixture_panel(n: int = 400) -> pl.DataFrame:
    stamps = _weekdays(n)
    up = _uptrend(n, seed=1)
    syn = _uptrend(n, seed=99)
    dead = _downtrend(n)
    return pl.concat(
        [
            _name_frame(DUP, stamps, up, volume=1_000_000.0, role="index_etf"),
            _name_frame(COPY, stamps, list(up), volume=1_000_000.0, role="index_etf"),
            _name_frame(SYN, stamps, syn, volume=1_000_000.0, role="commodity_etf"),
            _name_frame(TINY, stamps, list(up), volume=0.0, role="index_etf"),
            _name_frame(DEAD, stamps, dead, volume=1_000_000.0, role="index_etf"),
        ]
    )


def _last_diag(tmp_path: Path) -> pl.DataFrame:
    parquet = tmp_path / "prism-diagnostics.parquet"
    assert parquet.is_file(), "diagnostics=true should write $TMPDIR/prism-diagnostics.parquet"
    return pl.read_parquet(parquet)


def test_prism_source_does_not_import_journal_v6() -> None:
    import backtester.strategy.plugins.prism as mod

    source = Path(inspect.getsourcefile(mod) or "").read_text(encoding="utf-8")
    assert "v6_" not in source
    assert "v6_tsmom" not in source
    assert "v6_followups" not in source
    assert "strategy-backtest" not in source


def test_apply_hysteresis_does_not_flip_inside_band() -> None:
    raw = np.array([0.15, 0.05, -0.05, 0.09, -0.09, -0.15, -0.05, 0.05, 0.15], dtype=np.float64)
    trend = apply_hysteresis(raw, 0.10)
    assert trend[0] == pytest.approx(0.15)
    assert trend[1] == pytest.approx(0.15)
    assert trend[2] == pytest.approx(0.15)
    assert trend[3] == pytest.approx(0.15)
    assert trend[4] == pytest.approx(0.15)
    assert trend[5] == pytest.approx(0.0)
    assert trend[6] == pytest.approx(0.0)
    assert trend[7] == pytest.approx(0.0)
    assert trend[8] == pytest.approx(0.15)


def test_cluster_labels_duplicate_vs_diversifier() -> None:
    corr = np.array(
        [
            [1.0, 0.95, 0.05],
            [0.95, 1.0, 0.04],
            [0.05, 0.04, 1.0],
        ]
    )
    labels = cluster_labels(corr, 0.60)
    assert labels[0] == labels[1]
    assert labels[2] != labels[0]


def test_ledoit_wolf_keeps_duplicate_correlation() -> None:
    rng = np.random.default_rng(0)
    a = rng.normal(0.0, 0.01, size=80)
    returns = np.column_stack([a, a])
    cov, shrinkage = ledoit_wolf(returns)
    assert 0.0 <= shrinkage <= 1.0
    std = np.sqrt(np.diag(cov))
    corr = cov[0, 1] / (std[0] * std[1])
    assert corr == pytest.approx(1.0, abs=1e-9)


def test_prism_clusters_liquidity_trend_and_diagnostics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    panel = _fixture_panel()
    plugin = Prism()
    params = PrismParams(diagnostics=True)
    assert plugin.ready_bars(params) >= 252
    out = plugin.targets(panel, params)
    assert out.columns == ["ts_utc", "instrument_id", "weight"]
    assert out.height == panel.height

    diag = _last_diag(tmp_path)
    assert (tmp_path / "prism-diagnostics.csv").is_file()
    last_ts = diag["ts_utc"].max()
    last = diag.filter(pl.col("ts_utc") == last_ts)

    dup = last.filter(pl.col("instrument_id") == DUP).row(0, named=True)
    copy = last.filter(pl.col("instrument_id") == COPY).row(0, named=True)
    syn = last.filter(pl.col("instrument_id") == SYN).row(0, named=True)
    tiny = last.filter(pl.col("instrument_id") == TINY).row(0, named=True)
    dead = last.filter(pl.col("instrument_id") == DEAD).row(0, named=True)

    assert dup["cluster_id"] == copy["cluster_id"]
    assert syn["cluster_id"] is not None
    assert syn["cluster_id"] != dup["cluster_id"]
    assert dup["weight"] > 0
    assert copy["weight"] > 0
    assert syn["weight"] > 0
    assert tiny["weight"] == pytest.approx(0.0)
    assert tiny["eligible"] is False
    assert dead["trend"] == pytest.approx(0.0)
    assert dead["weight"] == pytest.approx(0.0)

    last_weights = out.filter(pl.col("ts_utc") == out["ts_utc"].max())
    gross = sum(w for w in last_weights["weight"].to_list() if w is not None)
    assert gross <= 1.0 + 1e-9
    ids = set(diag["instrument_id"].unique().to_list())
    assert ids == {DUP, COPY, SYN, TINY, DEAD}
    assert all("IRX" not in iid and not iid.endswith(":BIL") for iid in ids)


def test_prism_truncate_last_bar_leaves_prior_fills() -> None:
    panel = _fixture_panel()
    plugin = Prism()
    params = PrismParams()
    cfg = BrokerConfig(band_floor=0.005, band_frac=0.20, half_move=True, allow_short=False)
    targets = plugin.targets(panel, params)
    fills, _ = execute(panel, targets, cfg)
    last_ts = panel["ts_utc"].max()
    truncated = panel.filter(pl.col("ts_utc") < last_ts)
    targets_cut = plugin.targets(truncated, params)
    fills_cut, _ = execute(truncated, targets_cut, cfg)
    prior_targets = targets.filter(pl.col("ts_utc") < last_ts).sort(["ts_utc", "instrument_id"])
    got_targets = targets_cut.sort(["ts_utc", "instrument_id"])
    assert prior_targets.equals(got_targets)
    prior_fills = fills.filter(pl.col("ts_utc") < last_ts).sort(["ts_utc", "instrument_id"])
    got_fills = fills_cut.sort(["ts_utc", "instrument_id"])
    assert prior_fills.height > 0
    assert prior_fills.equals(got_fills)


def test_prism_no_cash_stays_fully_invested() -> None:
    panel = _fixture_panel()
    plugin = Prism()
    with_cash = plugin.targets(panel, PrismParams())
    no_cash = plugin.targets(panel, PrismParams(allow_cash=False))
    last_ts = panel["ts_utc"].max()
    cash_gross = sum(w for w in with_cash.filter(pl.col("ts_utc") == last_ts)["weight"].to_list() if w is not None)
    invested = sum(w for w in no_cash.filter(pl.col("ts_utc") == last_ts)["weight"].to_list() if w is not None)
    assert cash_gross <= 1.0 + 1e-9
    assert invested == pytest.approx(1.0, abs=1e-9)
    dead = no_cash.filter((pl.col("ts_utc") == last_ts) & (pl.col("instrument_id") == DEAD))
    assert float(dead["weight"][0]) == pytest.approx(0.0)


def test_prism_params_reject_unknown_keys() -> None:
    with pytest.raises(Exception, match="extra"):
        PrismParams.model_validate({"vol_target": 0.12, "v6": True})


def test_inverse_etf_gets_stock_haircut() -> None:
    assert _role_haircut("inverse_etf", 0.5) == pytest.approx(0.5)
    assert _role_haircut("stock", 0.5) == pytest.approx(0.5)
    assert _role_haircut("index_etf", 0.5) == pytest.approx(1.0)
    assert _role_haircut("commodity_etf", 0.5) == pytest.approx(1.0)
