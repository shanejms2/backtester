"""CLI wiring: --tz fail-fast, ingest/read/coverage against a temp store."""

from __future__ import annotations

from pathlib import Path

from backtester.cli import main
from backtester.store.parquet_duckdb import ParquetDuckDBStore
from tests.conftest import aapl, bars_frame, utc


def test_cli_rejects_ist(tmp_path: Path, monkeypatch, capsys) -> None:  # noqa: ANN001
    monkeypatch.setenv("BACKTESTER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ALPHAVANTAGE_API_KEY", raising=False)
    code = main(["read", "EQ:XNAS:AAPL", "--tf", "1d", "--from", "2024-01-01", "--to", "2024-02-01", "--tz", "IST"])
    assert code == 2
    err = capsys.readouterr().err
    assert "IANA" in err or "abbreviation" in err.lower() or "IST" in err


def test_cli_coverage(tmp_path: Path, monkeypatch, capsys) -> None:  # noqa: ANN001
    data = tmp_path / "data"
    monkeypatch.setenv("BACKTESTER_DATA_DIR", str(data))
    store = ParquetDuckDBStore(data)
    inst = aapl()
    store.upsert_instrument(inst)
    store.write_batch(bars_frame(inst, [utc(2024, 1, 16, 14, 30)], source="yahoo", timeframe="1d"))
    store.update_coverage(inst.instrument_id, "1d", "yahoo", utc(2024, 1, 16, 14, 30), utc(2024, 1, 16, 14, 30))
    store.close()
    code = main(["coverage", "EQ:XNAS:AAPL", "--tf", "1d", "--source", "yahoo"])
    assert code == 0
    out = capsys.readouterr().out
    assert "EQ:XNAS:AAPL" in out
