"""Chart HTML views. No browser, no live APIs."""

from __future__ import annotations

from pathlib import Path

import polars as pl

from backtester.cli import main
from backtester.inspect.chart import write_inventory_chart, write_series_chart
from backtester.store.clickhouse import ClickHouseStore
from tests.conftest import aapl, bars_frame, utc


def test_inventory_html_contains_coverage(tmp_store: ClickHouseStore, tmp_path: Path) -> None:
    inst = aapl()
    tmp_store.upsert_instrument(inst)
    start, end = utc(2024, 1, 2, 14, 30), utc(2024, 1, 16, 14, 30)
    tmp_store.write_batch(bars_frame(inst, [start, end], source="yahoo", timeframe="1d"))
    tmp_store.update_coverage(inst.instrument_id, "1d", "yahoo", start, end)
    path = write_inventory_chart(tmp_store, tz="UTC", open_browser=False, out_dir=tmp_path)
    html = path.read_text(encoding="utf-8")
    assert inst.instrument_id in html
    assert "2024-01-02" in html
    assert "2024-01-16" in html
    assert "Coverage inventory" in html


def test_series_html_marks_suspect(tmp_store: ClickHouseStore, tmp_path: Path) -> None:
    inst = aapl()
    tmp_store.upsert_instrument(inst)
    ts = utc(2024, 1, 16, 14, 30)
    frame = bars_frame(inst, [ts], source="yahoo", timeframe="1d")
    frame = frame.with_columns(pl.lit("suspect").alias("quality"))
    tmp_store.write_batch(frame)
    tmp_store.update_coverage(inst.instrument_id, "1d", "yahoo", ts, ts)
    path = write_series_chart(
        tmp_store,
        inst,
        "1d",
        utc(2024, 1, 1),
        utc(2024, 1, 20),
        tz="UTC",
        source="yahoo",
        open_browser=False,
        out_dir=tmp_path,
    )
    html = path.read_text(encoding="utf-8")
    assert inst.instrument_id in html
    assert "suspect" in html


def test_cli_chart_no_open(tmp_store: ClickHouseStore, monkeypatch, capsys) -> None:  # noqa: ANN001
    monkeypatch.setenv("CLICKHOUSE_DATABASE", tmp_store.database)
    inst = aapl()
    tmp_store.upsert_instrument(inst)
    ts = utc(2024, 1, 16, 14, 30)
    tmp_store.write_batch(bars_frame(inst, [ts], source="yahoo", timeframe="1d"))
    tmp_store.update_coverage(inst.instrument_id, "1d", "yahoo", ts, ts)
    code = main(
        [
            "chart",
            inst.instrument_id,
            "--tf",
            "1d",
            "--source",
            "yahoo",
            "--tz",
            "UTC",
            "--no-open",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out.strip()
    path = Path(out)
    assert path.exists()
    assert path.suffix == ".html"


def test_cli_chart_rejects_ist(tmp_path: Path, monkeypatch, capsys) -> None:  # noqa: ANN001
    monkeypatch.setenv("BACKTESTER_DATA_DIR", str(tmp_path / "data"))
    code = main(["chart", "--inventory", "--tz", "IST", "--no-open"])
    assert code == 2
    err = capsys.readouterr().err
    assert "IST" in err or "IANA" in err or "abbreviation" in err.lower()


def test_cli_inventory_no_open(tmp_store: ClickHouseStore, monkeypatch, capsys) -> None:  # noqa: ANN001
    monkeypatch.setenv("CLICKHOUSE_DATABASE", tmp_store.database)
    inst = aapl()
    tmp_store.update_coverage(inst.instrument_id, "1d", "yahoo", utc(2024, 1, 2), utc(2024, 1, 16))
    code = main(["chart", "--inventory", "--tz", "UTC", "--no-open"])
    assert code == 0
    html = Path(capsys.readouterr().out.strip()).read_text(encoding="utf-8")
    assert inst.instrument_id in html
    assert "2024-01-02" in html
