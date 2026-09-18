"""Coverage-first ingest: Request − Coverage − expected Gaps → fetch → Normalize → Store."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from backtester.adapters.alphavantage import AlphaVantageAdapter
from backtester.adapters.base import SourceAdapter
from backtester.adapters.ccxt_venue import CCXTVenueAdapter
from backtester.adapters.dexscreener import DexScreenerAdapter
from backtester.adapters.geckoterminal import GeckoTerminalAdapter
from backtester.adapters.yahoo import YahooAdapter
from backtester.calendar.display import ensure_utc, require_zone, to_utc
from backtester.calendar.session import SessionCalendar
from backtester.catalog.instruments import InstrumentCatalog
from backtester.domain import AssetClass, IngestRequest, IngestResult, Instrument, Interval
from backtester.exceptions import IncompleteHistoryError, QuotaError
from backtester.normalize.bars import normalize_vendor_bars, snapshots_to_frame
from backtester.settings import Settings, load_settings
from backtester.store.interface import BarStore
from backtester.timeframes import Timeframe

AdapterFactory = Callable[[str], SourceAdapter]

KNOWN_BAR_SOURCES = frozenset({"yahoo", "alphavantage", "geckoterminal"})
SNAPSHOT_SOURCE = "dexscreener"


class IngestEngine:
    """Deep ingest module. Adapters stay thin; hole math and writes live here."""

    def __init__(
        self,
        store: BarStore,
        *,
        catalog: InstrumentCatalog | None = None,
        calendar: SessionCalendar | None = None,
        settings: Settings | None = None,
        adapters: dict[str, SourceAdapter] | None = None,
        adapter_factory: AdapterFactory | None = None,
        now: Callable[[], datetime] | None = None,
        dex_resolver: DexScreenerAdapter | None = None,
    ) -> None:
        """Wire Store, Catalog, Calendar, and adapters.

        Args:
            store: BarStore seam (Parquet+DuckDB in v1).
            catalog: Instrument registry. Created on the store when omitted.
            calendar: Venue Session calendar.
            settings: Env-backed settings.
            adapters: Optional prebuilt adapters keyed by source id (tests).
            adapter_factory: Builds CCXT (or other) adapters for unknown source ids.
            now: Clock (tests freeze time).
            dex_resolver: DexScreener identity lookup for DEX Queries.
        """
        self._store = store
        self._catalog = catalog or InstrumentCatalog(store)
        self._calendar = calendar or SessionCalendar()
        self._settings = settings or load_settings()
        self._adapters = adapters or {}
        self._adapter_factory = adapter_factory
        self._now = now or (lambda: datetime.now(UTC))
        self._dex_resolver = dex_resolver

    def ingest(self, request: IngestRequest) -> IngestResult:
        """Fetch only Coverage holes, normalize, write, update Coverage.

        The same Request twice is a no-op once Coverage is complete.
        """
        zone = require_zone(request.tz or "UTC")
        start = to_utc(request.start, zone) if request.start.tzinfo is None else ensure_utc(request.start)
        end = to_utc(request.end, zone) if request.end.tzinfo is None else ensure_utc(request.end)
        instrument = self._resolve(request)
        source = self._route_source(instrument, request.source)
        if source == SNAPSHOT_SOURCE:
            return self._ingest_snapshots(instrument, request.timeframe, source)

        start, end = self._clip_pair_created(instrument, start, end)
        coverage = self._store.coverage(instrument.instrument_id, request.timeframe, source)
        holes = self._calendar.needed_ranges(
            instrument,
            request.timeframe,
            start,
            end,
            coverage,
            session=request.session,
        )
        if not holes:
            return IngestResult(
                instrument=instrument,
                source=source,
                timeframe=request.timeframe,
                rows_written=0,
                holes_fetched=0,
                noop=True,
            )

        adapter = self._adapter(source)
        rows = 0
        warnings: list[str] = []
        fetched = 0
        truncated = False
        try:
            for hole in holes:
                page_rows, hole_truncated = self._fill_hole(
                    adapter, instrument, request.timeframe, hole, request.session == "eth"
                )
                rows += page_rows
                fetched += 1
                truncated = truncated or hole_truncated
                if not hole_truncated:
                    self._store.update_coverage(
                        instrument.instrument_id,
                        request.timeframe,
                        source,
                        hole.start,
                        hole.end,
                    )
        except IncompleteHistoryError as exc:
            warnings.append(str(exc))
            truncated = True
        except QuotaError as exc:
            warnings.append(str(exc))
            truncated = True

        if adapter.truncated():
            truncated = True
            warnings.append(f"{source} truncated history; Coverage marked only for returned spans.")

        if truncated and rows:
            # Coverage for truncated vendors is the span actually written, not the hole.
            pass

        return IngestResult(
            instrument=instrument,
            source=source,
            timeframe=request.timeframe,
            rows_written=rows,
            holes_fetched=fetched,
            noop=rows == 0 and not warnings,
            warnings=tuple(warnings),
        )

    def _fill_hole(
        self,
        adapter: SourceAdapter,
        instrument: Instrument,
        timeframe: Timeframe,
        hole: Interval,
        extended_hours: bool,
    ) -> tuple[int, bool]:
        rows = 0
        min_ts: datetime | None = None
        max_ts: datetime | None = None
        for page in adapter.fetch_pages(
            instrument,
            timeframe,
            hole.start,
            hole.end,
            extended_hours=extended_hours,
        ):
            frame = normalize_vendor_bars(page, instrument, timeframe, now=self._now())
            if frame.is_empty():
                continue
            result = self._store.write_batch(frame)
            rows += result.rows_written
            ts = frame["ts_utc"]
            lo = ts.min()
            hi = ts.max()
            if isinstance(lo, datetime):
                min_ts = lo if min_ts is None else min(min_ts, lo)
            if isinstance(hi, datetime):
                max_ts = hi if max_ts is None else max(max_ts, hi)
        truncated = adapter.truncated()
        if truncated and min_ts is not None and max_ts is not None:
            self._store.update_coverage(
                instrument.instrument_id,
                timeframe,
                adapter.source_id,
                min_ts,
                max_ts,
            )
        return rows, truncated

    def _ingest_snapshots(self, instrument: Instrument, timeframe: Timeframe, source: str) -> IngestResult:
        adapter = self._adapter(source)
        snaps = adapter.fetch_snapshots(instrument)
        frame = snapshots_to_frame(snaps)
        written = 0
        if not frame.is_empty():
            written = self._store.write_snapshots(frame).rows_written
        return IngestResult(
            instrument=instrument,
            source=source,
            timeframe=timeframe,
            rows_written=written,
            holes_fetched=1 if written else 0,
            noop=written == 0,
        )

    def _resolve(self, request: IngestRequest) -> Instrument:
        if request.instrument_id:
            found = self._catalog.get(request.instrument_id)
            if found is not None:
                return found
        query = request.query
        asset_class = request.asset_class or _guess_class(query)
        if asset_class == "dex":
            existing = None
            try:
                existing = self._catalog.resolve(query, asset_class="dex", venue=request.venue)
            except Exception:
                existing = None
            if existing is not None and existing.pair_created_at is not None:
                return existing
            resolver = self._dex_resolver or self._maybe_dex()
            if resolver is not None:
                resolved = resolver.resolve(query)
                return self._catalog.register(resolved)
        return self._catalog.resolve(query, asset_class=asset_class, venue=request.venue)

    def _clip_pair_created(
        self,
        instrument: Instrument,
        start: datetime,
        end: datetime,
    ) -> tuple[datetime, datetime]:
        created = instrument.pair_created_at
        if created is None:
            return start, end
        created_utc = ensure_utc(created)
        return max(start, created_utc), end

    def _route_source(self, instrument: Instrument, source: str | None) -> str:
        if source:
            return source.lower()
        if instrument.asset_class == "equity":
            if self._settings.alphavantage_api_key is not None:
                return "alphavantage"
            return "yahoo"
        if instrument.asset_class == "crypto":
            return instrument.venue.lower() or "binance"
        return "geckoterminal"

    def _adapter(self, source: str) -> SourceAdapter:
        if source in self._adapters:
            return self._adapters[source]
        if self._adapter_factory is not None:
            built = self._adapter_factory(source)
            self._adapters[source] = built
            return built
        built = default_adapter(source, self._settings)
        self._adapters[source] = built
        return built

    def _maybe_dex(self) -> DexScreenerAdapter | None:
        existing = self._adapters.get("dexscreener")
        if isinstance(existing, DexScreenerAdapter):
            return existing
        if self._dex_resolver is not None:
            return self._dex_resolver
        built = default_adapter("dexscreener", self._settings)
        if isinstance(built, DexScreenerAdapter):
            self._adapters["dexscreener"] = built
            return built
        return None


def default_adapter(source: str, settings: Settings) -> SourceAdapter:
    """Construct a live adapter for ``source``."""
    if source == "yahoo":
        return YahooAdapter()
    if source == "alphavantage":
        key = settings.alphavantage_api_key
        if key is None:
            raise QuotaError("ALPHAVANTAGE_API_KEY is not set")
        return AlphaVantageAdapter(key, premium=settings.av_premium)
    if source == "dexscreener":
        return DexScreenerAdapter()
    if source == "geckoterminal":
        return GeckoTerminalAdapter()
    return CCXTVenueAdapter(source)


def _guess_class(query: str) -> AssetClass:
    lower = query.lower()
    if lower.startswith("dex:") or "dexscreener.com" in lower:
        return "dex"
    if "/" in query or lower.startswith("crypto:"):
        return "crypto"
    if lower.startswith("eq:"):
        return "equity"
    return "equity"
