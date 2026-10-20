"""Monthly interactive Compare webpage: $100 growth, allocation, per-name scorecard."""

from __future__ import annotations

import json
import os
import tempfile
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl

from backtester.exceptions import InvalidSpecError
from backtester.strategy.types import RunResult

_INSTRUMENT_LABELS = {
    "EQ:XNAS:QQQ": "QQQ",
    "EQ:XNSE:NIFTYBEES": "Nifty BeES",
    "EQ:ARCX:GLD": "GLD",
    "EQ:ARCX:SLV": "SLV",
    "CRYPTO:BINANCE:BTC/USDT": "BTC",
    "EQ:ARCX:XLE": "XLE",
    "EQ:ARCX:USO": "USO",
    "EQ:ARCX:PDBC": "PDBC",
    "EQ:XNAS:SQQQ": "SQQQ",
}
_RUN_LABELS = {
    "prism": "PRISM",
    "prism-nocash": "PRISM (no cash)",
    "qqq-gld": "QQQ/GLD",
    "qqq-gld-cash": "QQQ/GLD + cash",
    "qqq-gld-1997": "QQQ/GLD + cash",
    "qqq-gld-rise": "QQQ/GLD, rising",
    "nifty-winner": "Prior-year winner",
    "nifty-top3": "Three best",
    "nifty-quarter": "Three best, quarterly",
    "nifty-quarter-stop": "Quarterly, 200-day stop",
    "cap-mix": "3 large, 1 mid, 1 small",
    "cap-mix-333": "3 large, 3 mid, 3 small",
    "all-weather": "All weather",
    "qqq": "QQQ",
    "nifty": "Nifty BeES",
    "gld": "GLD",
    "slv": "SLV",
    "btc": "BTC",
    "xle": "XLE",
    "uso": "USO",
    "pdbc": "PDBC",
    "sqqq": "SQQQ",
}


def write_compare_page(
    spec_name: str,
    results: list[RunResult],
    *,
    open_browser: bool = True,
    out_dir: Path | None = None,
) -> Path:
    """Write a self-contained monthly HTML dashboard and optionally open it.

    Args:
        spec_name: Page title and file stem.
        results: Executed Runs sharing one Panel.
        open_browser: Open the file unless tests pass ``False``.
        out_dir: Directory for the file (default ``$TMPDIR``).

    Returns:
        Path to the HTML page.
    """
    if not results:
        raise InvalidSpecError("no Runs to chart")
    payload = _page_payload(spec_name, results)
    from plotly.offline import get_plotlyjs

    html = (
        _PAGE.replace("__SPEC__", spec_name)
        .replace("__PLOTLY_JS__", get_plotlyjs())
        .replace("__DATA__", json.dumps(payload, separators=(",", ":")))
    )
    folder = out_dir or Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"strat-compare-{spec_name}.html"
    path.write_text(html, encoding="utf-8")
    if open_browser:
        webbrowser.open(path.as_uri())
    return path


def _page_payload(spec_name: str, results: list[RunResult]) -> dict[str, Any]:
    align = max(r.metrics.start_ts for r in results)
    months: list[str] | None = None
    growth: dict[str, list[float]] = {}
    sip: dict[str, list[float]] = {}
    run_names: list[str] = []
    metrics_out: list[dict[str, Any]] = []
    for result in results:
        label = _run_label(result.run_id)
        run_names.append(label)
        frame = result.equity.filter(pl.col("ts_utc") >= align).sort("ts_utc")
        if frame.height == 0:
            continue
        stamps = frame["ts_utc"].to_list()
        equity = [float(v) for v in frame["equity"].to_list()]
        rets = [float(v) if v is not None else 0.0 for v in frame["r"].to_list()]
        base = equity[0] if equity and equity[0] else 1.0
        g100 = [round(100.0 * v / base, 2) for v in equity]
        sip_vals = [round(v, 2) for v in _sip_path(stamps, rets)]
        idxs = _month_end_indices(stamps)
        cats = [stamps[i].strftime("%Y-%m") for i in idxs]
        if months is None:
            months = cats
        growth[label] = [g100[i] for i in idxs]
        sip[label] = [sip_vals[i] for i in idxs]
        m = result.metrics
        metrics_out.append(
            {
                "name": label,
                "strategy": result.strategy,
                "growth100": round(m.growth_100, 2),
                "cagr": round(m.cagr * 100, 2),
                "maxdd": round(m.max_drawdown * 100, 2),
                "sipValue": round(m.sip_value, 2),
                "sipIn": round(m.sip_contributed, 2),
                "sipCagr": round(m.sip_cagr * 100, 2),
                "sipMaxdd": round(m.sip_max_drawdown * 100, 2),
                "vol": round(m.ann_vol * 100, 2),
                "sharpe": round(m.sharpe_excess, 2),
            }
        )
    if not months:
        raise InvalidSpecError("no equity rows to chart")
    allocations: dict[str, dict[str, Any]] = {}
    for result in results:
        if result.strategy not in {"prism", "guard", "year_winner"}:
            continue
        weights, alloc_names = _month_end_weights(result, align, months)
        if alloc_names:
            allocations[_run_label(result.run_id)] = {"names": alloc_names, "weights": weights}
    alloc_run = next((r for r in results if r.run_id == "prism"), None)
    if alloc_run is None:
        alloc_run = next((r for r in results if r.strategy == "prism"), results[0])
    default_label = _run_label(alloc_run.run_id)
    default = allocations.get(default_label)
    if default is None and allocations:
        default_label = next(iter(allocations))
        default = allocations[default_label]
    alloc_names = default["names"] if default else []
    weights = default["weights"] if default else {}
    primary = next((row for row in metrics_out if row["name"] == "PRISM"), None)
    if primary is None:
        primary = next((row for row in metrics_out if row["strategy"] == "prism"), metrics_out[0])
    return {
        "spec": spec_name,
        "from": str(align.date()),
        "to": str(results[0].metrics.end_ts.date()),
        "months": months,
        "runs": run_names,
        "growth100": growth,
        "sip": sip,
        "allocRun": default_label,
        "allocNames": alloc_names,
        "weights": weights,
        "allocations": allocations,
        "metrics": metrics_out,
        "primary": primary,
    }


def _run_label(run_id: str) -> str:
    return _RUN_LABELS.get(run_id, run_id)


def _instrument_label(instrument_id: str) -> str:
    if instrument_id in _INSTRUMENT_LABELS:
        return _INSTRUMENT_LABELS[instrument_id]
    symbol = instrument_id.rsplit(":", 1)[-1]
    return symbol.split("/")[0]


def _month_end_indices(stamps: list[datetime]) -> list[int]:
    last: dict[tuple[int, int], int] = {}
    for i, ts in enumerate(stamps):
        last[(ts.year, ts.month)] = i
    return [last[key] for key in sorted(last)]


def _sip_path(stamps: list[datetime], returns: list[float]) -> list[float]:
    wealth = 0.0
    prev: tuple[int, int] | None = None
    out: list[float] = []
    for ts, ret in zip(stamps, returns, strict=True):
        month = (ts.year, ts.month)
        add = 100.0 if month != prev else 0.0
        if add:
            prev = month
        wealth = wealth * (1.0 + ret) + add
        out.append(wealth)
    return out


def _month_end_weights(
    result: RunResult,
    align: datetime,
    months: list[str],
) -> tuple[dict[str, list[float]], list[str]]:
    equity = result.equity.filter(pl.col("ts_utc") >= align).sort("ts_utc")
    stamps = equity["ts_utc"].to_list()
    targets = result.targets.filter(pl.col("ts_utc") >= align)
    if targets.is_empty() or "weight" not in targets.columns:
        return {}, []
    wide = (
        targets.with_columns(pl.col("weight").cast(pl.Float64))
        .pivot(on="instrument_id", index="ts_utc", values="weight")
        .sort("ts_utc")
    )
    wide = equity.select("ts_utc").join(wide, on="ts_utc", how="left")
    name_cols = [c for c in wide.columns if c != "ts_utc"]
    if not name_cols:
        return {}, []
    wide = wide.with_columns([pl.col(c).forward_fill().fill_null(0.0) for c in name_cols])
    idxs = _month_end_indices(stamps)
    labels = [_instrument_label(c) for c in name_cols]
    # Keep first occurrence if two ids collapse to the same label.
    order: list[str] = []
    col_for: dict[str, str] = {}
    for col, label in zip(name_cols, labels, strict=True):
        if label not in col_for:
            col_for[label] = col
            order.append(label)
    weights: dict[str, list[float]] = {label: [] for label in order}
    weights["Cash"] = []
    for i in idxs:
        row = wide.row(i, named=True)
        gross = 0.0
        for label in order:
            val = max(0.0, float(row[col_for[label]] or 0.0))
            weights[label].append(round(val * 100.0, 1))
            gross += val
        weights["Cash"].append(round(max(0.0, 1.0 - gross) * 100.0, 1))
    # Re-index if the equity month-end list is longer than `months` (should match).
    if len(weights[order[0]]) != len(months):
        return {}, []
    return weights, [*order, "Cash"]


_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>__SPEC__ monthly</title>
  <style>
    :root { color-scheme: light; }
    * { box-sizing: border-box; }
    body {
      margin: 0; font-family: ui-sans-serif, system-ui, -apple-system, sans-serif;
      background: #f4f4f5; color: #18181b; line-height: 1.45;
    }
    header { background: #fff; border-bottom: 1px solid #e4e4e7; padding: 20px 28px 16px; }
    h1 { font-size: 22px; font-weight: 650; margin: 0 0 4px; }
    .sub { color: #71717a; font-size: 13px; }
    main { max-width: 1180px; margin: 0 auto; padding: 20px 28px 48px; }
    .stats { display: flex; flex-wrap: wrap; gap: 12px; margin: 16px 0 8px; }
    .stat { background: #fff; border: 1px solid #e4e4e7; border-radius: 8px; padding: 12px 16px; min-width: 140px; }
    .stat b { display: block; font-size: 20px; font-weight: 650; }
    .stat span { color: #71717a; font-size: 12px; }
    .panel { background: #fff; border: 1px solid #e4e4e7; border-radius: 8px; padding: 16px 16px 8px; margin-top: 16px; }
    h2 { font-size: 15px; font-weight: 650; margin: 0 0 4px; }
    .hint { color: #71717a; font-size: 12px; margin: 0 0 12px; }
    .row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 8px; }
    button {
      border: 1px solid #d4d4d8; background: #fff; border-radius: 999px;
      padding: 6px 12px; font-size: 13px; cursor: pointer; color: #18181b;
    }
    button.on { background: #18181b; color: #fff; border-color: #18181b; }
    .split { display: grid; grid-template-columns: 1fr 280px; gap: 16px; align-items: start; }
    @media (max-width: 860px) { .split { grid-template-columns: 1fr; } }
    input[type=range] { width: 100%; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #f4f4f5; }
    th { color: #71717a; font-weight: 500; font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
    td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
    tr.primary td { font-weight: 600; }
    .month-label { font-size: 18px; font-weight: 650; margin: 4px 0 8px; }
    #growth, #alloc, #pie { width: 100%; }
  </style>
</head>
<body>
  <header>
    <h1 id="title">Monthly review</h1>
    <div class="sub" id="subtitle"></div>
    <div class="stats" id="stats"></div>
  </header>
  <main>
    <section class="panel">
      <h2>Growth of $100</h2>
      <p class="hint">One point per month-end. Click a name in the legend to hide it. Hover a month to read every series.</p>
      <div class="row">
        <button id="btn-lump" class="on" type="button">Lump-sum $100</button>
        <button id="btn-sip" type="button">SIP $100 / month</button>
        <button id="btn-log" type="button">Log scale</button>
      </div>
      <div id="growth" style="height: 420px;"></div>
    </section>
    <section class="panel" id="alloc-panel">
      <h2>Asset allocation</h2>
      <p class="hint" id="alloc-hint">Month-end Target weights. A null Target holds the last weight. Cash is leftover under 100% gross.</p>
      <div class="row" id="alloc-picks"></div>
      <div id="alloc" style="height: 340px;"></div>
      <div class="split" style="margin-top: 12px;">
        <div>
          <div class="month-label" id="month-label"></div>
          <input id="month-slider" type="range" min="0" max="0" value="0"/>
          <div id="pie" style="height: 320px;"></div>
        </div>
        <div>
          <h2>Weights</h2>
          <table>
            <thead><tr><th>Sleeve</th><th class="num">Weight</th></tr></thead>
            <tbody id="weight-rows"></tbody>
          </table>
        </div>
      </div>
    </section>
    <section class="panel">
      <h2>How each name performed</h2>
      <p class="hint">Same aligned window as the growth chart. Buy-and-hold is a static 100% sleeve on that name.</p>
      <div style="overflow-x: auto;">
        <table>
          <thead>
            <tr>
              <th>Run</th>
              <th class="num">$100 lump</th>
              <th class="num">CAGR</th>
              <th class="num">Max DD</th>
              <th class="num">SIP end</th>
              <th class="num">SIP XIRR</th>
              <th class="num">SIP max DD</th>
              <th class="num">Vol</th>
              <th class="num">Sharpe</th>
            </tr>
          </thead>
          <tbody id="score-rows"></tbody>
        </table>
      </div>
    </section>
  </main>
  <script>__PLOTLY_JS__</script>
  <script>
    const DATA = __DATA__;
    let mode = "lump";
    let logScale = false;
    let monthIndex = DATA.months.length - 1;
    let allocKey = DATA.allocRun;
    const PALETTE = ["#2563eb","#059669","#d97706","#7c3aed","#db2777","#0f766e","#ca8a04","#ea580c","#4f46e5","#64748b"];

    document.title = DATA.spec + " monthly";
    document.getElementById("title").textContent = DATA.spec + " · monthly";
    document.getElementById("subtitle").textContent =
      DATA.from + " to " + DATA.to + " · month-end marks · click legend to toggle names";
    const p = DATA.primary;
    document.getElementById("stats").innerHTML = [
      stat(fmtMoney(p.growth100), p.name + " lump-sum $100"),
      stat(fmtPct(p.cagr), "CAGR"),
      stat(fmtPct(p.maxdd), "Max drawdown"),
      stat(fmtMoney(p.sipValue), "SIP $100/mo end"),
      stat(fmtPct(p.sipCagr), "SIP XIRR"),
    ].join("");

    document.getElementById("btn-lump").onclick = () => { mode = "lump"; syncBtns(); drawGrowth(); };
    document.getElementById("btn-sip").onclick = () => { mode = "sip"; syncBtns(); drawGrowth(); };
    document.getElementById("btn-log").onclick = () => { logScale = !logScale; syncBtns(); drawGrowth(); };

    const slider = document.getElementById("month-slider");
    slider.max = String(Math.max(0, DATA.months.length - 1));
    slider.value = String(monthIndex);
    slider.oninput = () => { monthIndex = Number(slider.value); drawMonth(); };

    fillScore();
    drawGrowth();
    const allocKeys = Object.keys(DATA.allocations || {});
    if (allocKeys.length) {
      const picks = document.getElementById("alloc-picks");
      allocKeys.forEach((key) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.textContent = key;
        btn.onclick = () => { allocKey = key; syncAllocBtns(); setAllocHint(); drawAlloc(); drawMonth(); };
        picks.appendChild(btn);
      });
      syncAllocBtns();
      setAllocHint();
      drawAlloc();
      drawMonth();
    } else {
      document.getElementById("alloc-panel").style.display = "none";
    }

    function syncBtns() {
      document.getElementById("btn-lump").className = mode === "lump" ? "on" : "";
      document.getElementById("btn-sip").className = mode === "sip" ? "on" : "";
      document.getElementById("btn-log").className = logScale ? "on" : "";
    }
    function syncAllocBtns() {
      const picks = document.getElementById("alloc-picks");
      Array.from(picks.children).forEach((btn) => {
        btn.className = btn.textContent === allocKey ? "on" : "";
      });
    }
    function currentAlloc() {
      const packed = (DATA.allocations || {})[allocKey];
      if (packed) return packed;
      return {names: DATA.allocNames, weights: DATA.weights};
    }
    function setAllocHint() {
      const cashNote = allocKey.indexOf("no cash") >= 0
        ? "ON names are rescaled to 100%; last book is held when every trend is off."
        : "Null Target holds. Cash is leftover under 100% gross.";
      document.getElementById("alloc-hint").textContent =
        allocKey + " month-end Target weights. " + cashNote;
    }
    function stat(value, label) {
      return '<div class="stat"><b>' + value + "</b><span>" + label + "</span></div>";
    }
    function fmtMoney(n) {
      return n.toLocaleString("en-US", {style:"currency", currency:"USD", maximumFractionDigits:0});
    }
    function fmtPct(n) {
      const abs = Math.abs(n).toFixed(1) + "%";
      return n > 0 ? "+" + abs : n < 0 ? "-" + abs : abs;
    }
    function drawGrowth() {
      const src = mode === "sip" ? DATA.sip : DATA.growth100;
      const traces = DATA.runs.map((name, i) => ({
        x: DATA.months,
        y: src[name],
        type: "scatter",
        mode: "lines+markers",
        name,
        line: {width: name === DATA.primary.name ? 2.6 : 1.6, color: PALETTE[i % PALETTE.length]},
        marker: {size: 5},
        hovertemplate: "%{fullData.name}: $%{y:.2f}<extra></extra>",
      }));
      const layout = {
        margin: {t: 16, r: 16, b: 56, l: 64},
        hovermode: "x unified",
        legend: {orientation: "h", y: 1.12, font: {size: 12}},
        xaxis: {title: "Month", tickangle: -45, nticks: 18, automargin: true},
        yaxis: {
          title: mode === "sip" ? "SIP wealth (USD)" : "Value of $100 (USD)",
          tickprefix: "$",
          type: logScale ? "log" : "linear",
        },
        paper_bgcolor: "#fff",
        plot_bgcolor: "#fafafa",
      };
      Plotly.react("growth", traces, layout, {responsive: true, displaylogo: false});
    }
    function drawAlloc() {
      const alloc = currentAlloc();
      const traces = alloc.names.map((name, i) => ({
        x: DATA.months,
        y: alloc.weights[name],
        type: "scatter",
        mode: "lines",
        stackgroup: "one",
        name,
        line: {width: 0.4},
        fillcolor: PALETTE[i % PALETTE.length],
        hovertemplate: "%{fullData.name}: %{y:.1f}%<extra></extra>",
      }));
      const layout = {
        margin: {t: 16, r: 16, b: 56, l: 48},
        hovermode: "x unified",
        legend: {orientation: "h", y: 1.14, font: {size: 12}},
        xaxis: {title: "Month", tickangle: -45, nticks: 18, automargin: true},
        yaxis: {title: "Weight", ticksuffix: "%", range: [0, 100]},
        paper_bgcolor: "#fff",
        plot_bgcolor: "#fafafa",
      };
      Plotly.react("alloc", traces, layout, {responsive: true, displaylogo: false});
    }
    function drawMonth() {
      const month = DATA.months[monthIndex];
      document.getElementById("month-label").textContent = month;
      const alloc = currentAlloc();
      const labels = [];
      const values = [];
      const rows = [];
      for (const name of alloc.names) {
        const v = alloc.weights[name][monthIndex];
        rows.push('<tr><td>' + name + '</td><td class="num">' + v.toFixed(1) + "%</td></tr>");
        if (v > 0.05) { labels.push(name); values.push(v); }
      }
      document.getElementById("weight-rows").innerHTML = rows.join("");
      Plotly.react("pie", [{
        type: "pie",
        labels,
        values,
        hole: 0.42,
        sort: false,
        textinfo: "label+percent",
        textposition: "inside",
        marker: {colors: labels.map((name) => PALETTE[alloc.names.indexOf(name) % PALETTE.length])},
        hovertemplate: "%{label}: %{value:.1f}%<extra></extra>",
      }], {
        margin: {t: 8, r: 8, b: 8, l: 8},
        showlegend: false,
        paper_bgcolor: "#fff",
      }, {responsive: true, displaylogo: false});
    }
    function fillScore() {
      const ranked = DATA.metrics.slice().sort((a, b) => b.growth100 - a.growth100);
      document.getElementById("score-rows").innerHTML = ranked.map((row) => {
        const cls = row.name === DATA.primary.name ? "primary" : "";
        return "<tr class='" + cls + "'><td>" + row.name + "</td>" +
          "<td class='num'>" + fmtMoney(row.growth100) + "</td>" +
          "<td class='num'>" + fmtPct(row.cagr) + "</td>" +
          "<td class='num'>" + fmtPct(row.maxdd) + "</td>" +
          "<td class='num'>" + fmtMoney(row.sipValue) + "</td>" +
          "<td class='num'>" + fmtPct(row.sipCagr) + "</td>" +
          "<td class='num'>" + fmtPct(row.sipMaxdd) + "</td>" +
          "<td class='num'>" + fmtPct(row.vol) + "</td>" +
          "<td class='num'>" + row.sharpe.toFixed(2) + "</td></tr>";
      }).join("");
    }
  </script>
</body>
</html>
"""
