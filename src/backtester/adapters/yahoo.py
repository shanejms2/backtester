"""Yahoo Finance adapter. Raw OHLC (auto_adjust=False). Thin vendor protocol."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from backtester.adapters.base import SourceAdapter
from backtester.calendar.display import ensure_utc
from backtester.domain import Instrument, VendorBar
from backtester.exceptions import NativeTimeframeError, PermanentVendorError, QuotaError
from backtester.timeframes import Timeframe

YahooDownload = Callable[..., Any]

YAHOO_INTERVAL: dict[Timeframe, str] = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "60m",
    "1d": "1d",
}

# Documented lookback caps (approximate). We still store whatever comes back.
YAHOO_LOOKBACK_DAYS: dict[Timeframe, int] = {
    "1m": 7,
    "5m": 60,
    "15m": 60,
    "30m": 60,
    "1h": 730,
    "1d": 36500,
}


class YahooAdapter(SourceAdapter):
    """Yahoo via yfinance. Source id ``yahoo``. Stores **raw** OHLC, not adjusted."""

    source_id = "yahoo"

    def __init__(self, download: YahooDownload | None = None) -> None:
        """Create the adapter.

        Args:
            download: Optional ``(symbol, start, end, interval, prepost) -> rows``
                injectable for tests. Rows are mappings or a pandas DataFrame.
        """
        self._download = download or _yfinance_download
        self._truncated = False

    def truncated(self) -> bool:
        """True when Yahoo's lookback cap likely clipped the Request."""
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
        """One page per Request (Yahoo paginates internally)."""
        if timeframe not in YAHOO_INTERVAL:
            raise NativeTimeframeError(
                f"yahoo has no native {timeframe}; fetch a finer Timeframe and resample on read."
            )
        self._truncated = False
        start_utc = ensure_utc(start)
        end_utc = ensure_utc(end)
        cap_days = YAHOO_LOOKBACK_DAYS[timeframe]
        if (end_utc - start_utc).days > cap_days:
            self._truncated = True
        interval = YAHOO_INTERVAL[timeframe]
        symbol = instrument.symbol
        try:
            raw = self._download(
                symbol,
                start=start_utc,
                end=end_utc,
                interval=interval,
                prepost=extended_hours,
            )
        except Exception as exc:
            text = str(exc).lower()
            if "429" in text or "too many" in text:
                raise QuotaError(f"yahoo rate-limited for {symbol}") from exc
            raise PermanentVendorError(f"yahoo download failed for {symbol}: {exc}") from exc
        bars = _rows_to_vendor(raw, symbol, timeframe)
        if not bars:
            return
        yield bars


def _yfinance_download(
    symbol: str,
    *,
    start: datetime,
    end: datetime,
    interval: str,
    prepost: bool,
) -> Any:
    import yfinance as yf

    ticker = yf.Ticker(symbol)
    return ticker.history(
        start=start.astimezone(UTC).replace(tzinfo=None),
        end=end.astimezone(UTC).replace(tzinfo=None),
        interval=interval,
        auto_adjust=False,
        prepost=prepost,
        actions=False,
    )


def _rows_to_vendor(raw: Any, symbol: str, timeframe: Timeframe) -> list[VendorBar]:
    rows = _iter_rows(raw)
    out: list[VendorBar] = []
    for row in rows:
        ts = _parse_ts(row)
        if ts is None:
            continue
        out.append(
            VendorBar(
                source="yahoo",
                vendor_symbol=symbol,
                timeframe=timeframe,
                ts=ts,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume") or 0.0),
                ts_kind="utc",
            )
        )
    return out


def _iter_rows(raw: Any) -> Sequence[Mapping[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    # pandas DataFrame
    if hasattr(raw, "empty") and raw.empty:
        return []
    if hasattr(raw, "reset_index"):
        frame = raw.reset_index()
        records: list[dict[str, Any]] = []
        for rec in frame.to_dict(orient="records"):
            lower = {str(k).lower(): v for k, v in rec.items()}
            ts = lower.get("date") or lower.get("datetime") or lower.get("index")
            records.append(
                {
                    "ts": ts,
                    "open": lower.get("open"),
                    "high": lower.get("high"),
                    "low": lower.get("low"),
                    "close": lower.get("close"),
                    "volume": lower.get("volume") or 0.0,
                }
            )
        return records
    return []


def _parse_ts(row: Mapping[str, Any]) -> datetime | None:
    ts = row.get("ts") or row.get("datetime") or row.get("date")
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=UTC)
    if hasattr(ts, "to_pydatetime"):
        converted = ts.to_pydatetime()
        if isinstance(converted, datetime):
            return converted if converted.tzinfo else converted.replace(tzinfo=UTC)
    if isinstance(ts, str):
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    if isinstance(ts, (int, float)):
        value = float(ts)
        if value > 1e12:
            value /= 1000.0
        return datetime.fromtimestamp(value, tz=UTC)
    return None
