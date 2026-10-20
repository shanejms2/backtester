"""CLI wiring: --tz fail-fast, ingest/read/coverage against ClickHouse."""

from __future__ import annotations

from pathlib import Path

from backtester.cli import main
from backtester.store.clickhouse import ClickHouseStore
from tests.conftest import aapl, bars_frame, utc


def test_cli_rejects_ist(tmp_path: Path, monkeypatch, capsys) -> None:  # noqa: ANN001
    monkeypatch.setenv("BACKTESTER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ALPHAVANTAGE_API_KEY", raising=False)
    code = main(["read", "EQ:XNAS:AAPL", "--tf", "1d", "--from", "2024-01-01", "--to", "2024-02-01", "--tz", "IST"])
    assert code == 2
    err = capsys.readouterr().err
    assert "IANA" in err or "abbreviation" in err.lower() or "IST" in err


def test_cli_coverage(tmp_store: ClickHouseStore, monkeypatch, capsys) -> None:  # noqa: ANN001
    monkeypatch.setenv("CLICKHOUSE_DATABASE", tmp_store.database)
    inst = aapl()
    tmp_store.upsert_instrument(inst)
    tmp_store.write_batch(bars_frame(inst, [utc(2024, 1, 16, 14, 30)], source="yahoo", timeframe="1d"))
    tmp_store.update_coverage(inst.instrument_id, "1d", "yahoo", utc(2024, 1, 16, 14, 30), utc(2024, 1, 16, 14, 30))
    code = main(["coverage", "EQ:XNAS:AAPL", "--tf", "1d", "--source", "yahoo"])
    assert code == 0
    out = capsys.readouterr().out
    assert "EQ:XNAS:AAPL" in out


def test_cli_ta_list(capsys) -> None:  # noqa: ANN001
    code = main(["ta", "list"])
    assert code == 0
    out = capsys.readouterr().out.splitlines()
    assert "sma" in out
    assert "rsi" in out
    assert "ewm_std" in out
    assert "swing_high" in out


def test_cli_strat_list(capsys) -> None:  # noqa: ANN001
    code = main(["strat", "list"])
    assert code == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["buy_hold", "cap_mix", "guard", "periodic", "prism", "sma_cross", "static", "year_winner"]


def test_cli_strat_run_missing_coverage(tmp_path: Path, monkeypatch, capsys, tmp_store: ClickHouseStore) -> None:  # noqa: ANN001
    monkeypatch.setenv("CLICKHOUSE_DATABASE", tmp_store.database)
    monkeypatch.setenv("BACKTESTER_DATA_DIR", str(tmp_path / "data"))
    yaml_path = tmp_path / "gap.yaml"
    yaml_path.write_text(
        """name: gap
timeframe: 1d
from: 2024-01-01
source: yahoo
universe:
  - id: EQ:XNAS:AAPL
    class: equity
    calendar: nyse
    fee_bps: 0
runs:
  - id: bh
    strategy: buy_hold
""",
        encoding="utf-8",
    )
    code = main(["strat", "run", str(yaml_path)])
    assert code == 2
    err = capsys.readouterr().err
    assert "ingest" in err
    assert "EQ:XNAS:AAPL" in err
