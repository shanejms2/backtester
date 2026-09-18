"""CLI: ingest, read --tz, coverage, compact."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from backtester.calendar.display import require_zone
from backtester.domain import IngestRequest
from backtester.exceptions import BacktesterError, IncompleteHistoryError
from backtester.ingest.engine import IngestEngine
from backtester.query.reader import BarReader
from backtester.settings import load_settings
from backtester.store.parquet_duckdb import ParquetDuckDBStore
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
    ingest.add_argument("--source", default=None, help="Vendor (yahoo, alphavantage, binance, geckoterminal, …).")
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


def _cmd_ingest(args: argparse.Namespace) -> int:
    settings = load_settings()
    tz = args.tz or settings.display_tz
    require_zone(tz)
    start, end = _parse_range(args.start, args.end, tz)
    store = ParquetDuckDBStore(Path(settings.data_dir))
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
    store = ParquetDuckDBStore(Path(settings.data_dir))
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
    store = ParquetDuckDBStore(Path(settings.data_dir))
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
                f"{item.instrument_id} {item.timeframe} {item.source} {item.start_utc.isoformat()} → {item.end_utc.isoformat()}"
            )
    return 0


def _cmd_compact(args: argparse.Namespace) -> int:
    settings = load_settings()
    store = ParquetDuckDBStore(Path(settings.data_dir))
    removed = store.compact(args.instrument)
    print(f"compacted files={removed}")
    if args.prune:
        pruned = store.prune_hot_1m(hot_days=settings.hot_1m_days)
        print(f"pruned_1m_files={pruned}")
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


def _infer_sources(store: ParquetDuckDBStore, instrument_id: str, timeframe: Timeframe) -> list[str]:
    # Probe common sources; Coverage rows are per source.
    found: list[str] = []
    for source in ("yahoo", "alphavantage", "binance", "geckoterminal", "dexscreener", "hyperliquid"):
        if store.coverage(instrument_id, timeframe, source):
            found.append(source)
    return found
