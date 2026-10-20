"""Strategy registry, plugins, Spec, Compare, warm-up. No live APIs, no journal scripts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest
from pydantic import ValidationError

from backtester.cli import main
from backtester.exceptions import InvalidSpecError, MissingCoverageError, UnknownStrategyError
from backtester.strategy.engine import compute_metrics, format_metrics_table, run_spec, write_equity_chart
from backtester.strategy.plugins.buy_hold import BuyHold, BuyHoldParams
from backtester.strategy.plugins.prism import Prism
from backtester.strategy.plugins.sma_cross import SmaCross, SmaCrossParams
from backtester.strategy.plugins.static import Static
from backtester.strategy.registry import get, list_strategies
from backtester.strategy.spec import load_spec
from backtester.ta import sma as catalog_sma
from tests.conftest import aapl, btc


def _stamps(n: int, start: datetime | None = None) -> list[datetime]:
    origin = start or datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    return [origin + timedelta(days=i) for i in range(n)]


def _ohlcv(
    instrument_id: str,
    stamps: list[datetime],
    *,
    source: str,
    timeframe: str,
    opens: list[float],
    closes: list[float],
) -> pl.DataFrame:
    highs = [max(o, c) + 0.5 for o, c in zip(opens, closes, strict=True)]
    lows = [min(o, c) - 0.5 for o, c in zip(opens, closes, strict=True)]
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
        }
    ).with_columns(pl.col("ts_utc").dt.convert_time_zone("UTC"))


def _seed(
    store: object,
    inst: object,
    stamps: list[datetime],
    *,
    source: str,
    opens: list[float],
    closes: list[float],
) -> None:
    store.upsert_instrument(inst)  # type: ignore[union-attr]
    store.write_batch(  # type: ignore[union-attr]
        _ohlcv(inst.instrument_id, stamps, source=source, timeframe="1d", opens=opens, closes=closes)
    )
    store.update_coverage(inst.instrument_id, "1d", source, stamps[0], stamps[-1])  # type: ignore[union-attr]


def _write_spec(
    path: Path,
    *,
    names: list[tuple[str, str, str, float]],
    runs: str,
    band: float = 0.0,
    allow_short: bool = True,
    source: str = "yahoo",
) -> Path:
    rows = "\n".join(
        f"  - id: {iid}\n    class: {cls}\n    calendar: {cal}\n    fee_bps: {fee}" for iid, cls, cal, fee in names
    )
    path.write_text(
        f"""name: fixture
timeframe: 1d
from: 2024-01-01
to: 2024-06-01
source: {source}
rebalance_band: {band}
allow_short: {str(allow_short).lower()}
universe:
{rows}
runs:
{runs}
""",
        encoding="utf-8",
    )
    return path


def test_registry_lists_seam_provers() -> None:
    names = list_strategies()
    assert names == ["buy_hold", "cap_mix", "guard", "periodic", "prism", "sma_cross", "static", "year_winner"]
    assert get("buy_hold") is BuyHold
    assert get("sma_cross") is SmaCross
    assert get("static") is Static
    assert get("prism") is Prism


def test_unknown_registry_name_fails_fast() -> None:
    with pytest.raises(UnknownStrategyError, match="nope"):
        get("nope")


def test_bad_params_fail_fast(tmp_store, tmp_path: Path) -> None:  # noqa: ANN001
    with pytest.raises(ValidationError):
        BuyHoldParams.model_validate({"foo": 1})
    with pytest.raises(ValidationError):
        SmaCrossParams.model_validate({"fast": 50, "slow": 20})
    with pytest.raises(ValidationError):
        SmaCrossParams.model_validate({"fast": 2, "slow": 5, "side": "both"})
    yaml_path = _write_spec(
        tmp_path / "params.yaml",
        names=[(aapl().instrument_id, "equity", "nyse", 0.0)],
        runs="  - id: bh\n    strategy: buy_hold\n    params: {foo: 1}\n",
    )
    with pytest.raises(InvalidSpecError, match="foo"):
        run_spec(load_spec(yaml_path), tmp_store)


def test_buy_hold_one_name_and_two_name_one_over_n() -> None:
    stamps = _stamps(4)
    one = pl.DataFrame(
        {
            "ts_utc": stamps,
            "instrument_id": [aapl().instrument_id] * 4,
            "open": [10.0] * 4,
            "high": [11.0] * 4,
            "low": [9.0] * 4,
            "close": [10.0] * 4,
            "volume": [100.0] * 4,
        }
    ).with_columns(pl.col("ts_utc").dt.convert_time_zone("UTC"))
    bh = BuyHold()
    w1 = bh.targets(one, BuyHoldParams())
    assert w1["weight"].to_list() == [1.0, 1.0, 1.0, 1.0]

    two = pl.concat(
        [
            one,
            one.with_columns(pl.lit(btc().instrument_id).alias("instrument_id")),
        ]
    )
    w2 = bh.targets(two, BuyHoldParams())
    assert w2["weight"].to_list() == [0.5] * 8


def test_sma_cross_calls_ta_sma(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    real = catalog_sma

    def wrapped(panel: pl.DataFrame, length: int = 20, col: str = "close") -> pl.Series:
        calls.append(length)
        return real(panel, length=length, col=col)

    monkeypatch.setattr("backtester.strategy.plugins.sma_cross.sma", wrapped)
    stamps = _stamps(8)
    panel = pl.DataFrame(
        {
            "ts_utc": stamps,
            "instrument_id": [aapl().instrument_id] * 8,
            "open": [float(10 + i) for i in range(8)],
            "high": [float(11 + i) for i in range(8)],
            "low": [float(9 + i) for i in range(8)],
            "close": [float(10 + i) for i in range(8)],
            "volume": [100.0] * 8,
        }
    ).with_columns(pl.col("ts_utc").dt.convert_time_zone("UTC"))
    plugin = SmaCross()
    params = SmaCrossParams(fast=2, slow=3, side="long_short")
    out = plugin.targets(panel, params)
    assert calls == [2, 3]
    assert out.columns == ["ts_utc", "instrument_id", "weight"]
    assert out["weight"][:2].null_count() == 2
    assert all(w in {-1.0, 0.0, 1.0} or w is None for w in out["weight"].to_list())


def test_spec_unknown_strategy_fails_at_run(tmp_store, tmp_path: Path) -> None:  # noqa: ANN001
    inst = aapl()
    stamps = _stamps(5)
    _seed(tmp_store, inst, stamps, source="yahoo", opens=[10.0] * 5, closes=[10.0] * 5)
    yaml_path = _write_spec(
        tmp_path / "bad.yaml",
        names=[(inst.instrument_id, "equity", "nyse", 0.0)],
        runs="  - id: x\n    strategy: nope\n",
    )
    spec = load_spec(yaml_path)
    with pytest.raises(UnknownStrategyError, match="nope"):
        run_spec(spec, tmp_store)


def test_missing_coverage_fails_with_ingest_command(tmp_store, tmp_path: Path) -> None:  # noqa: ANN001
    yaml_path = _write_spec(
        tmp_path / "gap.yaml",
        names=[(aapl().instrument_id, "equity", "nyse", 0.0)],
        runs="  - id: bh\n    strategy: buy_hold\n",
    )
    spec = load_spec(yaml_path)
    with pytest.raises(MissingCoverageError, match="backtester ingest") as exc:
        run_spec(spec, tmp_store)
    assert aapl().instrument_id in str(exc.value)
    assert "--tf 1d" in str(exc.value)


def test_warmup_metrics_start_at_ready_bars(tmp_store, tmp_path: Path) -> None:  # noqa: ANN001
    inst = aapl()
    stamps = _stamps(10)
    closes = [10.0 + (i % 4) for i in range(10)]
    opens = [10.0, *closes[:-1]]
    _seed(tmp_store, inst, stamps, source="yahoo", opens=opens, closes=closes)
    yaml_path = _write_spec(
        tmp_path / "warm.yaml",
        names=[(inst.instrument_id, "equity", "nyse", 0.0)],
        runs=("  - id: sma\n    strategy: sma_cross\n    params: {fast: 2, slow: 4, side: long_only}\n"),
    )
    result = run_spec(load_spec(yaml_path), tmp_store)[0]
    assert result.ready_bars == 4
    assert result.metrics.start_ts == stamps[4]
    assert result.metrics.ready_bars == 4
    assert result.metrics.growth_100 == pytest.approx(100.0 * (1.0 + result.metrics.total_return))


def test_compare_html_contains_both_run_ids(tmp_store, tmp_path: Path) -> None:  # noqa: ANN001
    inst = aapl()
    stamps = _stamps(12)
    closes = [10.0 + i * 0.3 if i < 6 else 13.0 - (i - 6) * 0.4 for i in range(12)]
    opens = [10.0, *closes[:-1]]
    _seed(tmp_store, inst, stamps, source="yahoo", opens=opens, closes=closes)
    yaml_path = _write_spec(
        tmp_path / "cmp.yaml",
        names=[(inst.instrument_id, "equity", "nyse", 0.0)],
        runs=(
            "  - id: bh\n"
            "    strategy: buy_hold\n"
            "  - id: sma\n"
            "    strategy: sma_cross\n"
            "    params: {fast: 2, slow: 3, side: long_short}\n"
        ),
    )
    results = run_spec(load_spec(yaml_path), tmp_store)
    assert {r.run_id for r in results} == {"bh", "sma"}
    path = write_equity_chart("fixture", results, open_browser=False, out_dir=tmp_path)
    html = path.read_text(encoding="utf-8")
    assert "bh" in html
    assert "sma" in html
    assert "Lump-sum $100" in html
    assert "SIP $100 / month" in html
    assert "month-end" in html


def test_two_name_buy_hold_engine(tmp_store, tmp_path: Path) -> None:  # noqa: ANN001
    eq, crypto = aapl(), btc()
    stamps = _stamps(6)
    closes = [10.0, 11.0, 12.0, 11.0, 12.0, 13.0]
    opens = [10.0, *closes[:-1]]
    _seed(tmp_store, eq, stamps, source="yahoo", opens=opens, closes=closes)
    _seed(tmp_store, crypto, stamps, source="yahoo", opens=opens, closes=closes)
    yaml_path = _write_spec(
        tmp_path / "two.yaml",
        names=[
            (eq.instrument_id, "equity", "nyse", 0.0),
            (crypto.instrument_id, "crypto", "native_24_7", 0.0),
        ],
        runs="  - id: bh\n    strategy: buy_hold\n",
    )
    result = run_spec(load_spec(yaml_path), tmp_store)[0]
    weights = set(result.targets["weight"].to_list())
    assert weights == {0.5}


def test_cli_compare_chart_no_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # noqa: ANN001
    from backtester.store.parquet_duckdb import ParquetDuckDBStore

    data = tmp_path / "data"
    monkeypatch.setenv("BACKTESTER_DATA_DIR", str(data))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    store = ParquetDuckDBStore(data)
    inst = aapl()
    stamps = _stamps(12)
    closes = [10.0 + i * 0.2 for i in range(12)]
    opens = [10.0, *closes[:-1]]
    _seed(store, inst, stamps, source="yahoo", opens=opens, closes=closes)
    store.close()
    yaml_path = _write_spec(
        tmp_path / "cli.yaml",
        names=[(inst.instrument_id, "equity", "nyse", 0.0)],
        runs=(
            "  - id: bh\n"
            "    strategy: buy_hold\n"
            "  - id: sma\n"
            "    strategy: sma_cross\n"
            "    params: {fast: 2, slow: 3, side: long_short}\n"
        ),
    )
    code = main(["strat", "compare", str(yaml_path), "--chart", "--no-open"])
    assert code == 0
    out = capsys.readouterr().out
    header = out.splitlines()[0]
    assert "CAGR" in header
    assert "$100" in header
    assert "SIP $100/mo" in header
    assert "SIP CAGR" in header
    assert "SIP maxdd" in header
    assert "bh" in out
    assert "sma" in out
    html_lines = [line.strip() for line in out.splitlines() if line.strip().endswith(".html")]
    assert html_lines
    html = Path(html_lines[-1]).read_text(encoding="utf-8")
    assert "bh" in html
    assert "sma" in html


def test_format_metrics_table_includes_cagr_and_growth_100() -> None:
    stamps = _stamps(3)
    equity = pl.DataFrame(
        {
            "ts_utc": stamps,
            "equity": [1.0, 1.5, 2.0],
            "r": [0.0, 0.5, 1.0 / 3.0],
            "turnover": [0.0, 0.0, 0.0],
        }
    ).with_columns(pl.col("ts_utc").dt.convert_time_zone("UTC"))
    fills = pl.DataFrame(
        {
            "ts_utc": pl.Series([], dtype=pl.Datetime(time_zone="UTC")),
            "instrument_id": pl.Series([], dtype=pl.Utf8),
            "size": pl.Series([], dtype=pl.Float64),
            "price": pl.Series([], dtype=pl.Float64),
            "fee": pl.Series([], dtype=pl.Float64),
        }
    )
    metrics = compute_metrics(
        run_id="bh",
        strategy_name="buy_hold",
        equity=equity,
        fills=fills,
        ready_bars=0,
        panel_ts=stamps,
    )
    assert metrics.total_return == pytest.approx(1.0)
    assert metrics.growth_100 == pytest.approx(200.0)
    assert metrics.cagr != 0.0
    table = format_metrics_table([metrics])
    header, row = table.splitlines()[0], table.splitlines()[1]
    assert "CAGR" in header
    assert "$100" in header
    assert "SIP $100/mo" in header
    assert "SIP in" in header
    assert "SIP x" in header
    assert "SIP CAGR" in header
    assert "SIP maxdd" in header
    assert "200.00" in row


def test_multi_name_static_per_name_runs(tmp_store, tmp_path: Path) -> None:  # noqa: ANN001
    eq, crypto = aapl(), btc()
    stamps = _stamps(6)
    closes = [10.0, 11.0, 12.0, 11.0, 12.0, 13.0]
    opens = [10.0, *closes[:-1]]
    _seed(tmp_store, eq, stamps, source="yahoo", opens=opens, closes=closes)
    _seed(tmp_store, crypto, stamps, source="yahoo", opens=opens, closes=closes)
    yaml_path = _write_spec(
        tmp_path / "per_name.yaml",
        names=[
            (eq.instrument_id, "equity", "nyse", 0.0),
            (crypto.instrument_id, "crypto", "native_24_7", 0.0),
        ],
        runs=(
            f"  - id: aapl\n"
            f"    strategy: static\n"
            f"    params:\n"
            f"      weights:\n"
            f'        "{eq.instrument_id}": 1.0\n'
            f"  - id: btc\n"
            f"    strategy: static\n"
            f"    params:\n"
            f"      weights:\n"
            f'        "{crypto.instrument_id}": 1.0\n'
        ),
    )
    results = run_spec(load_spec(yaml_path), tmp_store)
    assert [item.run_id for item in results] == ["aapl", "btc"]
    by_id = {item.run_id: item for item in results}
    aapl_w = set(by_id["aapl"].targets.filter(pl.col("instrument_id") == eq.instrument_id)["weight"].to_list())
    btc_w = set(by_id["btc"].targets.filter(pl.col("instrument_id") == crypto.instrument_id)["weight"].to_list())
    aapl_on_btc = set(by_id["aapl"].targets.filter(pl.col("instrument_id") == crypto.instrument_id)["weight"].to_list())
    assert aapl_w == {1.0}
    assert btc_w == {1.0}
    assert aapl_on_btc == {0.0}
    table = format_metrics_table([item.metrics for item in results])
    header = table.splitlines()[0]
    assert "CAGR" in header
    assert "$100" in header
    assert "SIP $100/mo" in header
    assert "SIP CAGR" in header
    assert "SIP maxdd" in header


def test_compare_aligns_metrics_to_latest_ready_bars(tmp_store, tmp_path: Path) -> None:  # noqa: ANN001
    inst = aapl()
    stamps = _stamps(10)
    closes = [10.0, 20.0, 30.0, 40.0, 40.0, 40.0, 40.0, 40.0, 40.0, 40.0]
    opens = [10.0, *closes[:-1]]
    _seed(tmp_store, inst, stamps, source="yahoo", opens=opens, closes=closes)
    yaml_path = _write_spec(
        tmp_path / "align.yaml",
        names=[(inst.instrument_id, "equity", "nyse", 0.0)],
        runs=(
            "  - id: bh\n"
            "    strategy: buy_hold\n"
            "  - id: sma\n"
            "    strategy: sma_cross\n"
            "    params: {fast: 2, slow: 4, side: long_only}\n"
        ),
    )
    results = run_spec(load_spec(yaml_path), tmp_store)
    by_id = {item.run_id: item for item in results}
    assert by_id["bh"].ready_bars == 0
    assert by_id["sma"].ready_bars == 4
    assert by_id["bh"].metrics.start_ts == stamps[4]
    assert by_id["sma"].metrics.start_ts == stamps[4]
    assert by_id["bh"].metrics.ready_bars == 4
    assert by_id["sma"].metrics.ready_bars == 4
    assert by_id["bh"].metrics.total_return == pytest.approx(0.0, abs=0.15)
    assert by_id["bh"].metrics.growth_100 == pytest.approx(100.0, abs=15.0)
