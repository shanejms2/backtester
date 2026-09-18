"""Plotly HTML Charts over BarReader. Temp files; not stored series."""

from __future__ import annotations

import os
import tempfile
import webbrowser
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import plotly.graph_objects as go
import polars as pl
from plotly.subplots import make_subplots

from backtester.calendar.display import ensure_utc
from backtester.calendar.session import SessionCalendar
from backtester.catalog.instruments import InstrumentCatalog
from backtester.domain import AssetClass, CoverageInterval, Instrument, SessionType
from backtester.inspect.problems import BarProblem, find_problems
from backtester.query.reader import BarReader
from backtester.store.interface import BarStore
from backtester.timeframes import DOWNSAMPLE_BAR_LIMIT, TIMEFRAME_DELTA, Timeframe, coarser_timeframe

_MARKER_COLOR = {
    "suspect": "#b45309",
    "partial": "#7c3aed",
    "zero_volume": "#0f766e",
    "spike": "#be123c",
}


def write_series_chart(
    store: BarStore,
    instrument: Instrument,
    timeframe: Timeframe,
    start: datetime,
    end: datetime,
    *,
    tz: str,
    source: str | None = None,
    session: SessionType = "rth",
    open_browser: bool = True,
    out_dir: Path | None = None,
) -> Path:
    """Read Bars, flag problems, write a self-contained candlestick HTML."""
    reader = BarReader(store)
    native = reader.read(
        instrument.instrument_id,
        timeframe,
        start,
        end,
        tz=tz,
        source=source,
    ).collect()
    plotted_tf: Timeframe = timeframe
    note = ""
    frame = native
    if native.height > DOWNSAMPLE_BAR_LIMIT:
        target = _downsample_target(timeframe, native.height)
        if target is not None:
            frame = reader.read(
                instrument.instrument_id,
                timeframe,
                start,
                end,
                tz=tz,
                resample=target,
                source=source,
            ).collect()
            plotted_tf = target
            note = f" downsampled {timeframe}→{target} ({native.height} bars)"
    problems = find_problems(
        native if plotted_tf == timeframe else frame,
        instrument,
        plotted_tf,
        calendar=SessionCalendar(),
        session=session,
    )
    fig = _series_figure(frame, instrument, plotted_tf, tz, problems, note=note)
    path = _write_html(fig, _series_stem(instrument, plotted_tf), out_dir)
    if open_browser:
        webbrowser.open(path.as_uri())
    return path


def write_inventory_chart(
    store: BarStore,
    *,
    tz: str,
    open_browser: bool = True,
    out_dir: Path | None = None,
) -> Path:
    """Coverage lanes for every stored series."""
    rows = store.list_coverage()
    fig = _inventory_figure(rows, tz)
    path = _write_html(fig, "coverage-inventory", out_dir)
    if open_browser:
        webbrowser.open(path.as_uri())
    return path


def resolve_chart_instrument(
    store: BarStore,
    query: str,
    *,
    asset_class: str | None = None,
    venue: str | None = None,
) -> Instrument:
    """Resolve a Chart query via the Catalog (same ids as ingest)."""
    catalog = InstrumentCatalog(store)
    hint: AssetClass | None = None
    if asset_class in {"equity", "crypto", "dex"}:
        hint = asset_class  # type: ignore[assignment]
    return catalog.resolve(query, asset_class=hint, venue=venue)


def coverage_span(
    store: BarStore,
    instrument_id: str,
    timeframe: Timeframe,
    source: str | None,
) -> tuple[datetime, datetime] | None:
    """Inclusive Coverage union for a series, or None if empty."""
    if source:
        items = store.coverage(instrument_id, timeframe, source)
    else:
        items = [c for c in store.list_coverage() if c.instrument_id == instrument_id and c.timeframe == timeframe]
    if not items:
        return None
    return min(c.start_utc for c in items), max(c.end_utc for c in items)


def _downsample_target(timeframe: Timeframe, n_bars: int) -> Timeframe | None:
    current: Timeframe | None = timeframe
    remaining = n_bars
    while current is not None and remaining > DOWNSAMPLE_BAR_LIMIT:
        nxt = coarser_timeframe(current)
        if nxt is None:
            return current if current != timeframe else None
        ratio = TIMEFRAME_DELTA[nxt] / TIMEFRAME_DELTA[current]
        remaining = max(1, int(remaining / ratio))
        current = nxt
    return current if current != timeframe else None


def _series_figure(
    frame: pl.DataFrame,
    instrument: Instrument,
    timeframe: Timeframe,
    tz: str,
    problems: list[BarProblem],
    *,
    note: str,
) -> go.Figure:
    ts_col = "ts_local" if "ts_local" in frame.columns else "ts_utc"
    if frame.is_empty():
        fig = go.Figure()
        fig.update_layout(title=f"{instrument.instrument_id} {timeframe} — no bars")
        return fig
    first = frame[ts_col][0]
    last = frame[ts_col][-1]
    n_gaps = sum(1 for p in problems if p.kind == "gap")
    title = (
        f"{instrument.instrument_id} {timeframe} · {frame.height} bars · "
        f"{first} → {last} ({tz}) · unexpected gaps={n_gaps}{note}"
    )
    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.72, 0.28],
        vertical_spacing=0.04,
    )
    fig.add_trace(
        go.Candlestick(
            x=frame[ts_col].to_list(),
            open=frame["open"].to_list(),
            high=frame["high"].to_list(),
            low=frame["low"].to_list(),
            close=frame["close"].to_list(),
            name="OHLC",
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Bar(x=frame[ts_col].to_list(), y=frame["volume"].to_list(), name="volume", marker_color="#94a3b8"),
        row=2,
        col=1,
    )
    zone = ZoneInfo(tz)
    high_by_ts = {
        ensure_utc(ts): float(high) for ts, high in zip(frame["ts_utc"].to_list(), frame["high"].to_list(), strict=True)
    }
    seen_kinds: set[str] = set()
    for problem in problems:
        if problem.kind == "gap":
            x0 = problem.ts_utc.astimezone(zone)
            x1 = problem.end_utc.astimezone(zone)
            fig.add_vrect(
                x0=x0,
                x1=x1,
                fillcolor="red",
                opacity=0.18,
                line_width=0,
                annotation_text="gap" if n_gaps < 12 else None,
                row=1,
                col=1,
            )
            continue
        color = _MARKER_COLOR.get(problem.kind, "#334155")
        x = problem.ts_utc.astimezone(zone)
        y = high_by_ts.get(problem.ts_utc)
        first_of_kind = problem.kind not in seen_kinds
        seen_kinds.add(problem.kind)
        fig.add_trace(
            go.Scatter(
                x=[x],
                y=[y],
                mode="markers",
                name=problem.kind,
                legendgroup=problem.kind,
                marker={"color": color, "size": 10, "symbol": "diamond"},
                hovertext=problem.label,
                showlegend=first_of_kind,
            ),
            row=1,
            col=1,
        )
    fig.update_layout(
        title=title,
        xaxis_rangeslider_visible=False,
        template="plotly_white",
        legend_traceorder="normal",
    )
    fig.update_yaxes(title_text="price", row=1, col=1)
    fig.update_yaxes(title_text="volume", row=2, col=1)
    return fig


def _inventory_figure(rows: list[CoverageInterval], tz: str) -> go.Figure:
    fig = go.Figure()
    zone = ZoneInfo(tz)
    if not rows:
        fig.update_layout(title="Coverage inventory — empty")
        return fig
    for item in rows:
        label = f"{item.instrument_id} {item.timeframe} {item.source}"
        start = ensure_utc(item.start_utc).astimezone(zone)
        end = ensure_utc(item.end_utc).astimezone(zone)
        fig.add_trace(
            go.Scatter(
                x=[start, end],
                y=[label, label],
                mode="lines",
                line={"width": 14},
                name=label,
                hovertext=f"{label}<br>{start} → {end}",
                showlegend=False,
            )
        )
    fig.update_layout(
        title=f"Coverage inventory · {len(rows)} lanes · {tz}",
        xaxis_title="time",
        yaxis_title="series",
        template="plotly_white",
    )
    return fig


def _series_stem(instrument: Instrument, timeframe: Timeframe) -> str:
    safe = instrument.instrument_id.replace(":", "-").replace("/", "-")
    return f"chart-{safe}-{timeframe}"


def _write_html(fig: go.Figure, stem: str, out_dir: Path | None) -> Path:
    folder = out_dir or Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stem}.html"
    fig.write_html(str(path), include_plotlyjs=True, full_html=True)
    return path
