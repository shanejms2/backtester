"""CLI: ingest, read --tz, coverage, compact, chart, ta, strat."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from backtester.calendar.display import require_zone
from backtester.domain import IngestRequest
from backtester.exceptions import BacktesterError, IncompleteHistoryError
from backtester.ingest.engine import IngestEngine
from backtester.inspect.chart import (
    coverage_span,
    resolve_chart_instrument,
    write_inventory_chart,
    write_series_chart,
)
from backtester.query.reader import BarReader
from backtester.settings import Settings, load_settings
from backtester.store.clickhouse import ClickHouseStore
from backtester.timeframes import Timeframe, parse_timeframe


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``backtester`` / ``python -m backtester``."""
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.handler(args))
    except BacktesterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backtester", description="Coverage-driven market data engine.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    ingest = sub.add_parser("ingest", help="Fetch Coverage holes for an Instrument.")
    ingest.add_argument("query", help="Ticker, id, CCXT symbol, chain:address, or DexScreener URL.")
    _add_common(ingest)
    ingest.add_argument(
        "--source",
        default=None,
        help="Vendor (yahoo, bhavcopy, alphavantage, binance, geckoterminal, …).",
    )
    ingest.add_argument(
        "--class",
        dest="asset_class",
        choices=("equity", "crypto", "dex"),
        default=None,
    )
    ingest.add_argument("--venue", default=None, help="MIC / CCXT id / chain.")
    ingest.add_argument("--session", choices=("rth", "eth"), default="rth")
    ingest.set_defaults(handler=_cmd_ingest)

    read = sub.add_parser("read", help="Print stored Bars (adds ts_local when --tz is set).")
    read.add_argument("instrument_id")
    _add_common(read)
    read.add_argument("--source", default=None)
    read.add_argument("--resample", default=None, help="Derived Timeframe (e.g. 1d from native 1m).")
    read.add_argument("--limit", type=int, default=20)
    read.set_defaults(handler=_cmd_read)

    cov = sub.add_parser("coverage", help="Show Coverage ranges.")
    cov.add_argument("instrument_id")
    cov.add_argument("--tf", required=True)
    cov.add_argument("--source", default=None)
    cov.add_argument("--tz", default=None)
    cov.set_defaults(handler=_cmd_coverage)

    compact = sub.add_parser("compact", help="Merge tiny Parquet parts; optionally prune old 1m.")
    compact.add_argument("--instrument", default=None)
    compact.add_argument("--prune", action="store_true", help="Drop old 1m after a native 1h/1d series exists.")
    compact.add_argument("--tz", default=None)
    compact.set_defaults(handler=_cmd_compact)

    chart = sub.add_parser("chart", help="Open a Plotly HTML Chart of stored Bars or Coverage inventory.")
    chart.add_argument("query", nargs="?", default=None, help="Instrument id or ticker (omit with --inventory).")
    chart.add_argument("--inventory", action="store_true", help="Coverage lanes for every stored series.")
    chart.add_argument("--tf", default=None, help="Native Timeframe (required unless --inventory).")
    chart.add_argument("--from", dest="start", default=None, help="ISO start (default: Coverage start).")
    chart.add_argument("--to", dest="end", default=None, help="ISO end (default: Coverage end).")
    chart.add_argument("--tz", default=None)
    chart.add_argument("--source", default=None)
    chart.add_argument("--venue", default=None)
    chart.add_argument(
        "--class",
        dest="asset_class",
        choices=("equity", "crypto", "dex"),
        default=None,
    )
    chart.add_argument("--session", choices=("rth", "eth"), default="rth")
    chart.add_argument("--no-open", action="store_true", help="Write HTML without opening a browser.")
    chart.set_defaults(handler=_cmd_chart)

    ta_p = sub.add_parser("ta", help="Technical analysis catalog.")
    ta_sub = ta_p.add_subparsers(dest="ta_cmd", required=True)
    ta_list = ta_sub.add_parser("list", help="Print Indicator names (same catalog as backtester.ta).")
    ta_list.set_defaults(handler=_cmd_ta_list)

    strat_p = sub.add_parser("strat", help="Run Strategies on a stored Panel.")
    strat_sub = strat_p.add_subparsers(dest="strat_cmd", required=True)
    strat_list = strat_sub.add_parser("list", help="Print registered Strategy names.")
    strat_list.set_defaults(handler=_cmd_strat_list)
    strat_run = strat_sub.add_parser("run", help="Execute every Run in a YAML Spec; print Metrics.")
    strat_run.add_argument("yaml", help="Path to Spec YAML.")
    strat_run.set_defaults(handler=_cmd_strat_run)
    strat_compare = strat_sub.add_parser("compare", help="Compare Runs; optional Plotly equity overlay.")
    strat_compare.add_argument("yaml", help="Path to Spec YAML.")
    strat_compare.add_argument("--chart", action="store_true", help="Write a Plotly equity overlay under $TMPDIR.")
    strat_compare.add_argument("--no-open", action="store_true", help="Write HTML without opening a browser.")
    strat_compare.set_defaults(handler=_cmd_strat_compare)

    serve = sub.add_parser("serve", help="Run the read-only JSON API (localhost; nginx for HTTPS).")
    serve.add_argument("--host", default=None, help="Bind host (default BACKTESTER_API_HOST or 127.0.0.1).")
    serve.add_argument("--port", type=int, default=None, help="Bind port (default BACKTESTER_API_PORT or 8088).")
    serve.set_defaults(handler=_cmd_serve)
    return parser


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tf", required=True, help="Native Timeframe: 1m, 5m, 15m, 30m, 1h, 4h, 1d.")
    parser.add_argument("--from", dest="start", required=True, help="ISO date or datetime.")
    parser.add_argument("--to", dest="end", default=None, help="ISO date or datetime (default: now UTC).")
    parser.add_argument(
        "--tz",
        default=None,
        help="IANA display / request zone (default BACKTESTER_TZ or UTC). Not IST/EDT.",
    )


@contextmanager
def _open_store(settings: Settings) -> Iterator[ClickHouseStore]:
    """Open the ClickHouse store and close the client when the command ends.

    Callers validate ``--tz`` before entering so a bad timezone never connects.
    """
    store = ClickHouseStore(
        host=settings.clickhouse_host,
        port=settings.clickhouse_port,
        username=settings.clickhouse_user,
        password=settings.clickhouse_password.get_secret_value(),
        database=settings.clickhouse_database,
    )
    try:
        yield store
    finally:
        store.close()


def _cmd_ingest(args: argparse.Namespace) -> int:
    settings = load_settings()
    tz = args.tz or settings.display_tz
    require_zone(tz)
    start, end = _parse_range(args.start, args.end, tz)
    with _open_store(settings) as store:
        engine = IngestEngine(store, settings=settings)
        request = IngestRequest(
            query=args.query,
            timeframe=parse_timeframe(args.tf),
            start=start,
            end=end,
            source=args.source,
            asset_class=args.asset_class,
            venue=args.venue,
            tz=tz,
            session=args.session,
        )
        try:
            result = engine.ingest(request)
        except IncompleteHistoryError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        extra = f" warnings={list(result.warnings)}" if result.warnings else ""
        print(
            f"{result.instrument.instrument_id} source={result.source} tf={result.timeframe} "
            f"rows={result.rows_written} holes={result.holes_fetched} noop={result.noop}{extra}"
        )
        if result.instrument.alternatives:
            print(f"alternatives: {list(result.instrument.alternatives)}")
        return 0


def _cmd_read(args: argparse.Namespace) -> int:
    settings = load_settings()
    tz = args.tz or settings.display_tz
    require_zone(tz)
    start, end = _parse_range(args.start, args.end, tz)
    with _open_store(settings) as store:
        reader = BarReader(store)
        lf = reader.read(
            args.instrument_id,
            parse_timeframe(args.tf),
            start,
            end,
            tz=tz,
            resample=args.resample,
            source=args.source,
        )
        frame = lf.collect()
        limit = args.limit
        print(frame.head(limit))
        print(f"rows={frame.height} tz={tz}")
        return 0


def _cmd_coverage(args: argparse.Namespace) -> int:
    settings = load_settings()
    with _open_store(settings) as store:
        tf = parse_timeframe(args.tf)
        sources = [args.source] if args.source else _infer_sources(store, args.instrument_id, tf)
        if not sources:
            print("no coverage")
            return 0
        for source in sources:
            ranges = store.coverage(args.instrument_id, tf, source)
            if not ranges:
                print(f"{args.instrument_id} {tf} {source}: (none)")
                continue
            for item in ranges:
                print(
                    f"{item.instrument_id} {item.timeframe} {item.source} "
                    f"{item.start_utc.isoformat()} → {item.end_utc.isoformat()}"
                )
        return 0


def _cmd_compact(args: argparse.Namespace) -> int:
    settings = load_settings()
    with _open_store(settings) as store:
        removed = store.compact(args.instrument)
        print(f"compacted files={removed}")
        if args.prune:
            pruned = store.prune_hot_1m(hot_days=settings.hot_1m_days)
            print(f"pruned_1m_files={pruned}")
        return 0


def _cmd_chart(args: argparse.Namespace) -> int:
    settings = load_settings()
    tz = args.tz or settings.display_tz
    require_zone(tz)
    with _open_store(settings) as store:
        if args.inventory:
            path = write_inventory_chart(store, tz=tz, open_browser=not args.no_open)
            print(path)
            return 0
        if not args.query or not args.tf:
            print("error: chart requires QUERY and --tf, or --inventory", file=sys.stderr)
            return 2
        instrument = resolve_chart_instrument(
            store,
            args.query,
            asset_class=args.asset_class,
            venue=args.venue,
        )
        tf = parse_timeframe(args.tf)
        span = coverage_span(store, instrument.instrument_id, tf, args.source)
        if args.start:
            start = _parse_dt(args.start, tz, is_end=False)
        elif span:
            start = span[0]
        else:
            print("error: no Coverage; pass --from", file=sys.stderr)
            return 2
        if args.end:
            end = _parse_dt(args.end, tz, is_end=True)
        elif span:
            end = span[1]
        else:
            end = datetime.now(UTC)
        path = write_series_chart(
            store,
            instrument,
            tf,
            start,
            end,
            tz=tz,
            source=args.source,
            session=args.session,
            open_browser=not args.no_open,
        )
        print(path)
        return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from backtester.api.app import run_server

    settings = load_settings()
    run_server(host=args.host, port=args.port, settings=settings)
    return 0


def _cmd_ta_list(_args: argparse.Namespace) -> int:
    from backtester.ta import available

    for name in available():
        print(name)
    return 0


def _cmd_strat_list(_args: argparse.Namespace) -> int:
    from backtester.strategy.registry import list_strategies

    for name in list_strategies():
        print(name)
    return 0


def _cmd_strat_run(args: argparse.Namespace) -> int:
    return _strat_execute(args.yaml, chart=False, open_browser=False)


def _cmd_strat_compare(args: argparse.Namespace) -> int:
    return _strat_execute(args.yaml, chart=bool(args.chart), open_browser=not args.no_open)


def _strat_execute(yaml_path: str, *, chart: bool, open_browser: bool) -> int:
    from backtester.strategy.engine import format_metrics_table, run_spec, write_equity_chart
    from backtester.strategy.spec import load_spec

    settings = load_settings()
    with _open_store(settings) as store:
        spec = load_spec(yaml_path)
        results = run_spec(spec, store)
        print(format_metrics_table([item.metrics for item in results]))
        if chart:
            path = write_equity_chart(
                spec.name,
                results,
                open_browser=open_browser,
            )
            print(path)
        return 0


def _parse_range(start_raw: str, end_raw: str | None, tz: str) -> tuple[datetime, datetime]:
    start = _parse_dt(start_raw, tz, is_end=False)
    end = _parse_dt(end_raw, tz, is_end=True) if end_raw else datetime.now(UTC)
    return start, end


def _parse_dt(raw: str, tz: str, *, is_end: bool) -> datetime:
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    date_only = len(text) == 10
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = datetime.strptime(text, "%Y-%m-%d")
        date_only = True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=require_zone(tz))
    if date_only and is_end:
        parsed = parsed + timedelta(days=1)
    return parsed


def _infer_sources(store: ClickHouseStore, instrument_id: str, timeframe: Timeframe) -> list[str]:
    # Probe common sources; Coverage rows are per source.
    found: list[str] = []
    for source in ("yahoo", "bhavcopy", "alphavantage", "binance", "geckoterminal", "dexscreener", "hyperliquid"):
        if store.coverage(instrument_id, timeframe, source):
            found.append(source)
    return found
