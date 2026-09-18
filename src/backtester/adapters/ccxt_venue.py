"""CCXT venue adapter. Unified symbols, enableRateLimit, since/limit pages."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime

from backtester.adapters.base import SourceAdapter
from backtester.calendar.display import ensure_utc
from backtester.domain import Instrument, VendorBar
from backtester.exceptions import PermanentVendorError, QuotaError
from backtester.timeframes import Timeframe

CCXT_PAGE = 500
FetchOHLCV = Callable[[str, str, int | None, int | None], list[list[float]]]


class CCXTVenueAdapter(SourceAdapter):
    """One CCXT exchange. Source id is the exchange id (``binance``, ``hyperliquid``)."""

    def __init__(
        self,
        exchange_id: str,
        *,
        fetch_ohlcv: FetchOHLCV | None = None,
        enable_rate_limit: bool = True,
    ) -> None:
        """Create the adapter.

        Args:
            exchange_id: CCXT id; becomes ``source`` on each Bar.
            fetch_ohlcv: Injectable ``(symbol, timeframe, since_ms, limit) -> klines``.
            enable_rate_limit: Passed to ccxt when constructing a live exchange.
        """
        self.exchange_id = exchange_id.lower()
        self.source_id = self.exchange_id
        self._fetch = fetch_ohlcv
        self._enable_rate_limit = enable_rate_limit
        self._truncated = False

    def truncated(self) -> bool:
        """CCXT pages until empty; truncated stays False unless the exchange errors mid-way."""
        return self._truncated

    def fetch_pages(
        self,
        instrument: Instrument,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        *,
        extended_hours: bool = False,
    ) -> Iterator[list[VendorBar]]:
        """Paginate with ``since`` + ``limit``, overlapping pages last-write-wins later."""
        del extended_hours
        start_utc = ensure_utc(start)
        end_utc = ensure_utc(end)
        since = int(start_utc.timestamp() * 1000)
        end_ms = int(end_utc.timestamp() * 1000)
        symbol = instrument.symbol
        fetch = self._fetch or self._live_fetch()
        last_since = -1
        while since < end_ms:
            try:
                raw = fetch(symbol, timeframe, since, CCXT_PAGE)
            except Exception as exc:
                text = str(exc).lower()
                if "429" in text or "rate" in text:
                    raise QuotaError(f"{self.source_id} rate-limited") from exc
                raise PermanentVendorError(f"{self.source_id} fetch_ohlcv failed: {exc}") from exc
            if not raw:
                break
            bars = [
                VendorBar(
                    source=self.source_id,
                    vendor_symbol=symbol,
                    timeframe=timeframe,
                    ts=datetime.fromtimestamp(row[0] / 1000.0, tz=UTC),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                    ts_kind="utc",
                )
                for row in raw
                if len(row) >= 6 and start_utc.timestamp() * 1000 <= row[0] < end_ms
            ]
            if bars:
                yield bars
            next_since = int(raw[-1][0]) + 1
            if next_since <= last_since or next_since <= since:
                break
            last_since = since
            since = next_since
            if len(raw) < CCXT_PAGE // 4:
                break

    def _live_fetch(self) -> FetchOHLCV:
        import ccxt

        factory = getattr(ccxt, self.exchange_id, None)
        if factory is None:
            raise PermanentVendorError(f"unknown ccxt exchange {self.exchange_id!r}")
        exchange = factory({"enableRateLimit": self._enable_rate_limit})

        def _call(symbol: str, timeframe: str, since: int | None, limit: int | None) -> list[list[float]]:
            return list(exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit))

        return _call
