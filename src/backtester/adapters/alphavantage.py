"""Alpha Vantage adapter. Quota Note JSON, Eastern naive timestamps, month paging."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import SecretStr

from backtester.adapters.base import SourceAdapter
from backtester.calendar.display import ensure_utc
from backtester.domain import Instrument, VendorBar
from backtester.exceptions import IncompleteHistoryError, NativeTimeframeError, PermanentVendorError, QuotaError
from backtester.timeframes import Timeframe

AV_BASE = "https://www.alphavantage.co/query"
AV_INTERVAL: dict[Timeframe, str] = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "60min",
}

_SERIES_KEYS = (
    "Time Series (Daily)",
    "Time Series (1min)",
    "Time Series (5min)",
    "Time Series (15min)",
    "Time Series (30min)",
    "Time Series (60min)",
    "Time Series Crypto (Daily)",
)


class AlphaVantageBudget:
    """Process-wide free-tier budget so two ingests cannot stampede the key."""

    def __init__(self, daily_limit: int = 25, per_minute: int = 5) -> None:
        self.daily_limit = daily_limit
        self.per_minute = per_minute
        self._lock = threading.Lock()
        self._day: datetime | None = None
        self._count = 0
        self._stamps: list[float] = []

    def acquire(self) -> None:
        """Block for the per-minute cap; raise QuotaError when the daily cap is hit."""
        while True:
            with self._lock:
                now = datetime.now(UTC)
                day = now.replace(hour=0, minute=0, second=0, microsecond=0)
                if self._day != day:
                    self._day = day
                    self._count = 0
                    self._stamps = []
                if self._count >= self.daily_limit:
                    raise QuotaError(
                        f"alphavantage daily budget exhausted ({self.daily_limit}/day). "
                        "Coverage was not marked complete."
                    )
                cutoff = time.monotonic() - 60.0
                self._stamps = [s for s in self._stamps if s >= cutoff]
                if len(self._stamps) < self.per_minute:
                    self._stamps.append(time.monotonic())
                    self._count += 1
                    return
                wait = 60.0 - (time.monotonic() - self._stamps[0]) + 0.05
            time.sleep(max(wait, 0.05))


_PROCESS_BUDGET = AlphaVantageBudget()


class AlphaVantageAdapter(SourceAdapter):
    """Equity TIME_SERIES_* plus optional DIGITAL_CURRENCY_DAILY.

    Rate-limit and premium-upsell often return HTTP 200 with JSON ``Note`` or
    ``Information``. Those are quota errors, not empty series.
    """

    source_id = "alphavantage"

    def __init__(
        self,
        api_key: SecretStr | str,
        *,
        client: httpx.Client | None = None,
        budget: AlphaVantageBudget | None = None,
        premium: bool = False,
    ) -> None:
        """Create the adapter.

        Args:
            api_key: Secret; never logged.
            client: Optional httpx client (tests inject MockTransport).
            budget: Shared process budget. ``None`` uses the module singleton.
            premium: When True, ``outputsize=full`` on daily and CRYPTO_INTRADAY.
        """
        if isinstance(api_key, SecretStr):
            self._key = api_key.get_secret_value()
        else:
            self._key = api_key
        self._client = client or httpx.Client(base_url=AV_BASE, timeout=30.0)
        self._owns_client = client is None
        self._budget = budget or _PROCESS_BUDGET
        self._premium = premium
        self._truncated = False

    def truncated(self) -> bool:
        """True when compact daily was used or history was otherwise clipped."""
        return self._truncated

    def close(self) -> None:
        """Close the owned HTTP client."""
        if self._owns_client:
            self._client.close()

    def fetch_pages(
        self,
        instrument: Instrument,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        *,
        extended_hours: bool = False,
    ) -> Iterator[list[VendorBar]]:
        """Page by ``month=YYYY-MM`` for intraday; one call for daily."""
        self._truncated = False
        start_utc = ensure_utc(start)
        end_utc = ensure_utc(end)
        symbol = instrument.symbol
        if instrument.asset_class == "crypto":
            yield from self._crypto_pages(symbol, timeframe, start_utc, end_utc)
            return
        if timeframe == "4h":
            raise NativeTimeframeError("alphavantage has no native 4h; ingest 1h and resample on read.")
        if timeframe == "1d":
            yield from self._daily_pages(symbol, start_utc, end_utc)
            return
        if timeframe not in AV_INTERVAL:
            raise NativeTimeframeError(f"alphavantage cannot serve {timeframe}")
        yield from self._intraday_pages(symbol, timeframe, start_utc, end_utc, extended_hours=extended_hours)

    def _daily_pages(self, symbol: str, start: datetime, end: datetime) -> Iterator[list[VendorBar]]:
        outputsize = "full" if self._premium else "compact"
        payload = self._get(
            {
                "function": "TIME_SERIES_DAILY",
                "symbol": symbol,
                "outputsize": outputsize,
            }
        )
        bars = _parse_series(payload, "alphavantage", symbol, "1d", ts_kind="session_date")
        bars = [b for b in bars if start <= _aware(b.ts) < end or _session_in_range(b, start, end)]
        span_days = (end - start).days
        if outputsize == "compact" and span_days > 160:
            self._truncated = True
            if bars:
                yield bars
            raise IncompleteHistoryError(
                "alphavantage outputsize=full is required for this daily range but is premium; "
                "refusing to mark Coverage complete for the requested span."
            )
        if bars:
            yield bars

    def _intraday_pages(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        *,
        extended_hours: bool,
    ) -> Iterator[list[VendorBar]]:
        interval = AV_INTERVAL[timeframe]
        cursor = datetime(start.year, start.month, 1, tzinfo=UTC)
        last = datetime(end.year, end.month, 1, tzinfo=UTC)
        while cursor <= last:
            month = f"{cursor.year:04d}-{cursor.month:02d}"
            payload = self._get(
                {
                    "function": "TIME_SERIES_INTRADAY",
                    "symbol": symbol,
                    "interval": interval,
                    "month": month,
                    "outputsize": "full",
                    "adjusted": "false",
                    "extended_hours": "true" if extended_hours else "false",
                }
            )
            bars = _parse_series(payload, "alphavantage", symbol, timeframe, ts_kind="naive_session")
            clipped = [b for b in bars if start <= _naive_as_placeholder(b.ts, start) < end]
            if clipped:
                yield clipped
            if cursor.month == 12:
                cursor = datetime(cursor.year + 1, 1, 1, tzinfo=UTC)
            else:
                cursor = datetime(cursor.year, cursor.month + 1, 1, tzinfo=UTC)

    def _crypto_pages(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> Iterator[list[VendorBar]]:
        base = symbol.split("/")[0]
        if timeframe != "1d":
            if not self._premium:
                raise NativeTimeframeError(
                    "alphavantage CRYPTO_INTRADAY is premium; default crypto Requests stay on CCXT."
                )
            raise NativeTimeframeError("CRYPTO_INTRADAY is not used unless explicitly premium-enabled.")
        payload = self._get(
            {
                "function": "DIGITAL_CURRENCY_DAILY",
                "symbol": base,
                "market": "USD",
            }
        )
        bars = _parse_series(payload, "alphavantage", symbol, "1d", ts_kind="session_date")
        clipped = [b for b in bars if start <= _aware(b.ts) < end or _session_in_range(b, start, end)]
        if clipped:
            yield clipped

    def _get(self, params: dict[str, str]) -> dict[str, Any]:
        self._budget.acquire()
        query = {**params, "apikey": self._key}
        response = self._client.get("", params=query)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise PermanentVendorError("alphavantage returned non-object JSON")
        if "Note" in payload:
            raise QuotaError(f"alphavantage Note: {payload['Note']}")
        if "Information" in payload:
            raise QuotaError(f"alphavantage Information: {payload['Information']}")
        if "Error Message" in payload:
            raise PermanentVendorError(f"alphavantage Error Message: {payload['Error Message']}")
        return payload


def _parse_series(
    payload: dict[str, Any],
    source: str,
    symbol: str,
    timeframe: Timeframe,
    *,
    ts_kind: str,
) -> list[VendorBar]:
    series = None
    for key in _SERIES_KEYS:
        if key in payload:
            series = payload[key]
            break
    if series is None:
        for key, value in payload.items():
            if key == "Meta Data":
                continue
            if isinstance(value, dict):
                series = value
                break
    if not isinstance(series, dict):
        return []
    out: list[VendorBar] = []
    for stamp, fields in series.items():
        if not isinstance(fields, dict):
            continue
        ts = _parse_stamp(stamp)
        ohlc = _ohlc_from_fields(fields)
        if ohlc is None:
            continue
        out.append(
            VendorBar(
                source=source,
                vendor_symbol=symbol,
                timeframe=timeframe,
                ts=ts,
                open=ohlc[0],
                high=ohlc[1],
                low=ohlc[2],
                close=ohlc[3],
                volume=ohlc[4],
                ts_kind=ts_kind,  # type: ignore[arg-type]
            )
        )
    return out


def _ohlc_from_fields(fields: dict[str, Any]) -> tuple[float, float, float, float, float] | None:
    def grab(*names: str) -> float | None:
        for name in names:
            if name in fields:
                try:
                    return float(fields[name])
                except (TypeError, ValueError):
                    return None
        return None

    open_ = grab("1. open", "open")
    high = grab("2. high", "high")
    low = grab("3. low", "low")
    close = grab("4. close", "close")
    volume = grab("5. volume", "volume", "6. volume") or 0.0
    if open_ is None or high is None or low is None or close is None:
        return None
    return open_, high, low, close, float(volume)


def _parse_stamp(stamp: str) -> datetime:
    text = stamp.strip()
    if " " in text:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    return datetime.strptime(text, "%Y-%m-%d")


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _naive_as_placeholder(ts: datetime, start: datetime) -> datetime:
    """Filter helper: treat naive stamps as UTC placeholders; Normalize does ET→UTC."""
    if ts.tzinfo is None:
        # Session-local; month paging already selected the right month.
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def _session_in_range(bar: VendorBar, start: datetime, end: datetime) -> bool:
    ts = bar.ts
    if ts.tzinfo is None:
        day = ts.date()
        return start.date() <= day <= (end - timedelta(microseconds=1)).date()
    aware = ts.astimezone(UTC)
    return start <= aware < end
