"""Decision-clock Panel, T-bill cash overlay, static Strategy. No live APIs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from backtester.domain import Instrument
from backtester.strategy.engine import compute_metrics, run_spec
from backtester.strategy.panel import load_panel, scale_tbill_by_calendar_days
from backtester.strategy.plugins.static import Static, StaticParams
from backtester.strategy.spec import load_spec
from tests.conftest import aapl, btc

NY = ZoneInfo("America/New_York")
IRX = "EQ:XNAS:IRX"


def _ohlcv(
    instrument_id: str,
    stamps: list[datetime],
    *,
    source: str,
    timeframe: str,
    opens: list[float],
    closes: list[float],
    adj_close: list[float] | None = None,
) -> pl.DataFrame:
    highs = [max(o, c) + 0.5 for o, c in zip(opens, closes, strict=True)]
    lows = [min(o, c) - 0.5 for o, c in zip(opens, closes, strict=True)]
    adj = adj_close if adj_close is not None else list(closes)
    return pl.DataFrame(
        {
            "instrument_id": [instrument_id] * len(stamps),
            "source": source,
            "timeframe": timeframe,
            "ts_utc": stamps,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [100.0] * len(stamps),
            "is_partial": [False] * len(stamps),
            "quality": ["ok"] * len(stamps),
            "adj_close": adj,
        }
    ).with_columns(pl.col("ts_utc").dt.convert_time_zone("UTC"))


def _seed(
    store: object,
    inst: object,
    stamps: list[datetime],
    *,
    source: str,
    timeframe: str,
    opens: list[float],
    closes: list[float],
    adj_close: list[float] | None = None,
) -> None:
    store.upsert_instrument(inst)  # type: ignore[union-attr]
    store.write_batch(  # type: ignore[union-attr]
        _ohlcv(
            inst.instrument_id,  # type: ignore[union-attr]
            stamps,
            source=source,
            timeframe=timeframe,
            opens=opens,
            closes=closes,
            adj_close=adj_close,
        )
    )
    store.update_coverage(inst.instrument_id, timeframe, source, stamps[0], stamps[-1])  # type: ignore[union-attr]


def test_nyse_1600_two_crypto_hours_one_close(tmp_store, tmp_path: Path) -> None:  # noqa: ANN001
    crypto = btc()
    # Wednesday 2024-01-03 is an NYSE session; Saturday 2024-01-06 is not.
    open_bar = datetime(2024, 1, 3, 9, 0, tzinfo=NY).astimezone(UTC)
    close_bar = datetime(2024, 1, 3, 15, 0, tzinfo=NY).astimezone(UTC)
    weekend = datetime(2024, 1, 6, 15, 0, tzinfo=NY).astimezone(UTC)
    stamps = [open_bar, close_bar, weekend]
    _seed(
        tmp_store,
        crypto,
        stamps,
        source="binance",
        timeframe="1h",
        opens=[100.0, 105.0, 200.0],
        closes=[101.0, 110.0, 201.0],
    )
    yaml_path = tmp_path / "clock.yaml"
    yaml_path.write_text(
        """name: clock
timeframe: 1d
from: 2024-01-03
to: 2024-01-08
source: binance
clock: nyse_1600
universe:
  - id: CRYPTO:BINANCE:BTC/USDT
    class: crypto
    calendar: native_24_7
    fee_bps: 0
    source: binance
runs:
  - id: bh
    strategy: buy_hold
""",
        encoding="utf-8",
    )
    spec = load_spec(yaml_path)
    panel = load_panel(spec, tmp_store)
    assert panel.height == 1
    decision = datetime(2024, 1, 3, 16, 0, tzinfo=NY).astimezone(UTC)
    assert panel["ts_utc"][0] == decision
    assert float(panel["close"][0]) == pytest.approx(110.0)
    assert float(panel["open"][0]) == pytest.approx(100.0)
    assert float(panel["adj_close"][0]) == pytest.approx(110.0)
    saturday = datetime(2024, 1, 6, 16, 0, tzinfo=NY).astimezone(UTC)
    assert saturday not in panel["ts_utc"].to_list()


def test_nyse_1600_inner_join_etf_and_crypto(tmp_store, tmp_path: Path) -> None:  # noqa: ANN001
    eq, crypto = aapl(), btc()
    etf_open = datetime(2024, 1, 3, 9, 30, tzinfo=NY).astimezone(UTC)
    _seed(
        tmp_store,
        eq,
        [etf_open],
        source="yahoo",
        timeframe="1d",
        opens=[50.0],
        closes=[52.0],
        adj_close=[48.0],
    )
    open_bar = datetime(2024, 1, 3, 9, 0, tzinfo=NY).astimezone(UTC)
    close_bar = datetime(2024, 1, 3, 15, 0, tzinfo=NY).astimezone(UTC)
    _seed(
        tmp_store,
        crypto,
        [open_bar, close_bar],
        source="binance",
        timeframe="1h",
        opens=[100.0, 105.0],
        closes=[101.0, 110.0],
    )
    yaml_path = tmp_path / "mix.yaml"
    yaml_path.write_text(
        """name: mix
timeframe: 1d
from: 2024-01-03
to: 2024-01-04
source: yahoo
clock: nyse_1600
universe:
  - id: EQ:XNAS:AAPL
    class: equity
    calendar: nyse
    fee_bps: 5
    role: index_etf
    source: yahoo
  - id: CRYPTO:BINANCE:BTC/USDT
    class: crypto
    calendar: native_24_7
    fee_bps: 15
    role: crypto
    source: binance
runs:
  - id: bh
    strategy: buy_hold
""",
        encoding="utf-8",
    )
    panel = load_panel(load_spec(yaml_path), tmp_store)
    by_id = {row["instrument_id"]: row for row in panel.to_dicts()}
    assert set(by_id) == {eq.instrument_id, crypto.instrument_id}
    assert float(by_id[eq.instrument_id]["close"]) == pytest.approx(52.0)
    assert float(by_id[eq.instrument_id]["adj_close"]) == pytest.approx(48.0)
    assert float(by_id[crypto.instrument_id]["close"]) == pytest.approx(110.0)
    assert by_id[eq.instrument_id]["ts_utc"] == by_id[crypto.instrument_id]["ts_utc"]
    assert by_id[eq.instrument_id]["role"] == "index_etf"
    assert IRX not in panel["instrument_id"].to_list()


def test_scale_tbill_by_calendar_days_weekend_gap() -> None:
    """Fri→Mon must earn three calendar days, not one panel row."""
    fri = datetime(2024, 1, 5, 21, 0, tzinfo=UTC)
    mon = datetime(2024, 1, 8, 21, 0, tzinfo=UTC)
    frame = pl.DataFrame(
        {
            "ts_utc": [fri, mon],
            "tbill_per_day": [0.01, 0.01],
        }
    ).with_columns(pl.col("ts_utc").dt.convert_time_zone("UTC"))
    out = scale_tbill_by_calendar_days(frame)
    assert float(out["daily_tbill"][0]) == pytest.approx(0.0)
    assert float(out["daily_tbill"][1]) == pytest.approx(0.03)


def test_cash_rate_all_cash_run_compounds(tmp_store, tmp_path: Path) -> None:  # noqa: ANN001
    eq = aapl()
    stamps = [datetime(2024, 1, 2, 14, 30, tzinfo=UTC) + timedelta(days=i) for i in range(4)]
    _seed(tmp_store, eq, stamps, source="yahoo", timeframe="1d", opens=[10.0] * 4, closes=[10.0] * 4)
    irx = Instrument(
        instrument_id=IRX,
        asset_class="equity",
        venue="XNAS",
        symbol="IRX",
        session_tz="America/New_York",
        aliases=("^IRX",),
    )
    _seed(tmp_store, irx, stamps, source="yahoo", timeframe="1d", opens=[365.0] * 4, closes=[365.0] * 4)
    yaml_path = tmp_path / "cash.yaml"
    yaml_path.write_text(
        f"""name: cash
timeframe: 1d
from: 2024-01-01
to: 2024-01-10
source: yahoo
cash_rate_id: {IRX}
universe:
  - id: {eq.instrument_id}
    class: equity
    calendar: nyse
    fee_bps: 0
runs:
  - id: cash
    strategy: static
    params:
      weights:
        "{eq.instrument_id}": 0.0
""",
        encoding="utf-8",
    )
    spec = load_spec(yaml_path)
    panel = load_panel(spec, tmp_store)
    assert IRX not in panel["instrument_id"].to_list()
    assert all("IRX" not in iid and not str(iid).endswith(":BIL") for iid in panel["instrument_id"].unique())
    # First stamp accrues 0; the next three consecutive calendar days each earn 1%.
    assert float(panel["daily_tbill"][0]) == pytest.approx(0.0)
    assert float(panel["daily_tbill"][1]) == pytest.approx(0.01)
    result = run_spec(spec, tmp_store)[0]
    assert IRX not in result.targets["instrument_id"].to_list()
    assert result.fills.height == 0
    assert float(result.equity["equity"][-1]) == pytest.approx(1.01**3)
    assert result.metrics.mean_cash == pytest.approx(1.0)
    assert result.metrics.fill_count == 0


def test_static_weights_map() -> None:
    stamps = [datetime(2024, 1, 2, 14, 30, tzinfo=UTC) + timedelta(days=i) for i in range(3)]
    a = aapl().instrument_id
    b = btc().instrument_id
    panel = pl.DataFrame(
        {
            "ts_utc": stamps + stamps,
            "instrument_id": [a] * 3 + [b] * 3,
            "open": [10.0] * 6,
            "high": [11.0] * 6,
            "low": [9.0] * 6,
            "close": [10.0] * 6,
            "volume": [1.0] * 6,
        }
    ).with_columns(pl.col("ts_utc").dt.convert_time_zone("UTC"))
    out = Static().targets(panel, StaticParams(weights={a: 1.0}))
    by_id = out.group_by("instrument_id").agg(pl.col("weight").first())
    got = {row["instrument_id"]: row["weight"] for row in by_id.to_dicts()}
    assert got[a] == pytest.approx(1.0)
    assert got[b] == pytest.approx(0.0)


def test_metrics_best_worst_year_and_labels() -> None:
    stamps = [
        datetime(2023, 12, 29, 21, 0, tzinfo=UTC),
        datetime(2024, 1, 2, 21, 0, tzinfo=UTC),
        datetime(2024, 1, 3, 21, 0, tzinfo=UTC),
    ]
    equity = pl.DataFrame(
        {
            "ts_utc": stamps,
            "equity": [1.0, 1.10, 1.0],
            "r": [0.0, 0.10, -0.090909],
            "turnover": [0.0, 0.5, 0.0],
            "gross": [1.0, 1.0, 1.0],
            "cash_weight": [0.0, 0.0, 0.0],
            "daily_tbill": [0.0, 0.0, 0.0],
        }
    ).with_columns(pl.col("ts_utc").dt.convert_time_zone("UTC"))
    fills = pl.DataFrame(
        {
            "ts_utc": [stamps[1]],
            "instrument_id": ["EQ:XNAS:AAPL"],
            "size": [1.0],
            "price": [10.0],
            "fee": [0.0],
        }
    ).with_columns(pl.col("ts_utc").dt.convert_time_zone("UTC"))
    metrics = compute_metrics(
        run_id="m",
        strategy_name="static",
        equity=equity,
        fills=fills,
        ready_bars=0,
        panel_ts=stamps,
    )
    assert metrics.fill_count == 1
    assert metrics.sharpe_raw == pytest.approx(metrics.sharpe_excess)
    assert metrics.best_year in {2023, 2024}
    assert metrics.worst_year in {2023, 2024}
    assert metrics.mean_gross == pytest.approx(1.0)
    assert metrics.mean_cash == pytest.approx(0.0)
    assert metrics.annual_turnover >= 0.0
    assert metrics.cagr is not None
    assert metrics.growth_100 == pytest.approx(100.0 * (1.0 + metrics.total_return))
    assert metrics.sortino is not None
    assert metrics.calmar is not None


def test_nse_bar_maps_to_india_session_date(tmp_store, tmp_path: Path) -> None:  # noqa: ANN001
    """00:00 IST Wednesday must join NYSE Wednesday, not Tuesday (NY conversion)."""
    ist = ZoneInfo("Asia/Kolkata")
    bees = Instrument(
        instrument_id="EQ:XNSE:NIFTYBEES",
        asset_class="equity",
        venue="XNSE",
        symbol="NIFTYBEES",
        session_tz="Asia/Kolkata",
        aliases=("NIFTYBEES.NS",),
    )
    native = datetime(2024, 1, 3, 0, 0, tzinfo=ist).astimezone(UTC)
    _seed(tmp_store, bees, [native], source="yahoo", timeframe="1d", opens=[250.0], closes=[252.0])
    yaml_path = tmp_path / "nse.yaml"
    yaml_path.write_text(
        """name: nse
timeframe: 1d
from: 2024-01-02
to: 2024-01-04
source: yahoo
clock: nyse_1600
universe:
  - id: EQ:XNSE:NIFTYBEES
    class: equity
    calendar: nse
    fee_bps: 5
    role: index_etf
    source: yahoo
runs:
  - id: bh
    strategy: buy_hold
""",
        encoding="utf-8",
    )
    panel = load_panel(load_spec(yaml_path), tmp_store)
    decision = datetime(2024, 1, 3, 16, 0, tzinfo=NY).astimezone(UTC)
    assert decision in panel["ts_utc"].to_list()
    row = panel.filter(pl.col("ts_utc") == decision)
    assert float(row["close"][0]) == pytest.approx(252.0)


def test_inr_prices_convert_to_usd(tmp_store, tmp_path: Path) -> None:  # noqa: ANN001
    ist = ZoneInfo("Asia/Kolkata")
    bees = Instrument(
        instrument_id="EQ:XNSE:NIFTYBEES",
        asset_class="equity",
        venue="XNSE",
        symbol="NIFTYBEES",
        session_tz="Asia/Kolkata",
        aliases=("NIFTYBEES.NS",),
    )
    fx = Instrument(
        instrument_id="EQ:CCY:INR",
        asset_class="equity",
        venue="CCY",
        symbol="INR",
        session_tz="UTC",
        aliases=("INR=X",),
    )
    native = datetime(2024, 1, 3, 0, 0, tzinfo=ist).astimezone(UTC)
    fx_ts = datetime(2024, 1, 3, 0, 0, tzinfo=UTC)
    _seed(tmp_store, bees, [native], source="yahoo", timeframe="1d", opens=[250.0], closes=[250.0])
    _seed(tmp_store, fx, [fx_ts], source="yahoo", timeframe="1d", opens=[80.0], closes=[80.0])
    yaml_path = tmp_path / "fx.yaml"
    yaml_path.write_text(
        """name: fx
timeframe: 1d
from: 2024-01-02
to: 2024-01-04
source: yahoo
clock: nyse_1600
fx:
  INR: EQ:CCY:INR
universe:
  - id: EQ:XNSE:NIFTYBEES
    class: equity
    calendar: nse
    fee_bps: 5
    role: index_etf
    currency: INR
    source: yahoo
runs:
  - id: bh
    strategy: buy_hold
""",
        encoding="utf-8",
    )
    panel = load_panel(load_spec(yaml_path), tmp_store)
    assert float(panel["close"][0]) == pytest.approx(250.0 / 80.0)
    assert float(panel["open"][0]) == pytest.approx(250.0 / 80.0)
    assert panel["currency"][0] == "INR"
