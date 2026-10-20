"""NSE cash bhavcopy. One official daily file for the whole market, saved raw before parsing."""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Iterator, Sequence
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from backtester.adapters.base import SourceAdapter
from backtester.calendar.display import ensure_utc
from backtester.calendar.session import SessionCalendar
from backtester.domain import Instrument, VendorBar
from backtester.exceptions import NativeTimeframeError, PermanentVendorError, QuotaError
from backtester.timeframes import Timeframe

# First session NSE published in the UDiFF cash file. Earlier sessions use cmDDMONYYYYbhav.csv.zip.
_UDIFF_START = date(2024, 7, 8)
_IST = ZoneInfo("Asia/Kolkata")
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/",
}
# Header text NSE has used. A new spelling must be added here; the parser does not guess.
_COLUMNS: dict[str, tuple[str, ...]] = {
    "symbol": ("symbol", "tckrsymb"),
    "series": ("series", "sctysrs"),
    "open": ("open", "openprice", "opnpric"),
    "high": ("high", "highprice", "hghpric"),
    "low": ("low", "lowprice", "lwpric"),
    "close": ("close", "closeprice", "clspric"),
    "volume": ("tottrdqty", "ttltrdqnty", "ttltradgvol"),
}


class BhavcopyAdapter(SourceAdapter):
    """NSE cash daily bars from the official bhavcopy.

    Each session is one zip for every listed name. The zip is written under
    ``raw_dir`` before any column is read, so a parser change can rebuild
    bars from those files. Close is the exchange close, not a split-adjusted
    series: ``adj_close`` is set equal to ``close``.
    """

    source_id = "bhavcopy"

    def __init__(
        self,
        raw_dir: Path,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        """Save official files under ``raw_dir`` and reuse them on the next read.

        Args:
            raw_dir: Directory for the zip bytes (``data/raw/bhavcopy``).
            client: Optional HTTP client. Tests pass one that must not be called
                when the zip is already on disk.
        """
        self._raw_dir = Path(raw_dir)
        self._client = client or httpx.Client(timeout=40.0, headers=_HEADERS, follow_redirects=True)
        self._owns = client is None
        self._truncated = False

    def truncated(self) -> bool:
        """True when a download failed after some days were already parsed."""
        return self._truncated

    def close(self) -> None:
        """Close the owned HTTP client."""
        if self._owns:
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
        """Yield EQ-series bars for NSE session days in ``[start, end)``."""
        del extended_hours
        if timeframe != "1d":
            raise NativeTimeframeError(f"bhavcopy is NSE cash daily only; got {timeframe}")
        if instrument.venue.upper() != "XNSE":
            raise PermanentVendorError(f"bhavcopy only covers NSE cash; venue is {instrument.venue}")
        self._truncated = False
        symbol = instrument.symbol.upper()
        dates = SessionCalendar().xnse_session_dates(ensure_utc(start), ensure_utc(end))
        page: list[VendorBar] = []
        for day in dates:
            try:
                payload = self.cache_day(day)
            except (QuotaError, PermanentVendorError):
                self._truncated = True
                if page:
                    yield page
                raise
            if payload is None:
                continue
            bar = bar_from_bhavcopy(payload, symbol, day)
            if bar is None:
                continue
            page.append(bar)
            if len(page) >= 250:
                yield page
                page = []
        if page:
            yield page

    def cache_day(self, day: date) -> bytes | None:
        """Return the official zip, downloading it once into ``raw_dir``.

        A missing session (HTTP 404 on every known URL) returns None. The file
        already on disk is returned without a request.
        """
        name = official_name(day)
        path = self._raw_dir / f"{day:%Y}" / f"{day:%m}" / name
        if path.is_file() and path.stat().st_size > 0:
            return path.read_bytes()
        payload = self._download(day)
        if payload is None:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return payload

    def _download(self, day: date) -> bytes | None:
        saw_not_found = False
        for url in official_urls(day):
            response = self._client.get(url)
            if response.status_code == 404:
                saw_not_found = True
                continue
            if response.status_code in {429, 503}:
                raise QuotaError(f"bhavcopy HTTP {response.status_code} for {day.isoformat()}")
            if response.status_code != 200 or not response.content:
                raise PermanentVendorError(f"bhavcopy HTTP {response.status_code} for {day.isoformat()} {url}")
            return response.content
        if saw_not_found:
            return None
        raise PermanentVendorError(f"bhavcopy has no URL for {day.isoformat()}")


def official_name(day: date) -> str:
    """File name NSE uses for this session."""
    if day >= _UDIFF_START:
        return f"BhavCopy_NSE_CM_0_0_0_{day:%Y%m%d}_F_0000.csv.zip"
    month = day.strftime("%b").upper()
    return f"cm{day:%d}{month}{day:%Y}bhav.csv.zip"


def official_urls(day: date) -> tuple[str, ...]:
    """Candidate URLs, newest layout first for post-cutover sessions."""
    name = official_name(day)
    month = day.strftime("%b").upper()
    legacy = (
        f"https://archives.nseindia.com/content/historical/EQUITIES/{day:%Y}/{month}/{name}",
        f"https://nsearchives.nseindia.com/content/historical/EQUITIES/{day:%Y}/{month}/{name}",
    )
    udiff = (f"https://nsearchives.nseindia.com/content/cm/{name}",)
    if day >= _UDIFF_START:
        return udiff + legacy
    return legacy + udiff


def bar_from_bhavcopy(payload: bytes, symbol: str, day: date) -> VendorBar | None:
    """Read the EQ row for ``symbol`` out of one official zip or CSV.

    Args:
        payload: Zip or CSV bytes as NSE published them.
        symbol: NSE ticker, compared case-insensitively.
        day: Session date of this file. The bar opens at 09:15 IST.

    Returns:
        The EQ bar, or None when that ticker did not print in series EQ.

    Raises:
        PermanentVendorError: The file has no usable OHLC columns. The bytes
            should already be on disk so the map can be fixed and replayed.
    """
    text = _csv_text(payload)
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise PermanentVendorError(f"bhavcopy {day.isoformat()} has no header")
    fields = _bind_columns(reader.fieldnames, day)
    wanted = symbol.upper()
    for row in reader:
        series = (row.get(fields["series"]) or "").strip().upper()
        ticker = (row.get(fields["symbol"]) or "").strip().upper()
        if ticker != wanted or series != "EQ":
            continue
        prices = _prices(row, fields)
        if prices is None:
            continue
        open_, high, low, close, volume = prices
        opened = datetime(day.year, day.month, day.day, 9, 15, tzinfo=_IST)
        return VendorBar(
            source="bhavcopy",
            vendor_symbol=ticker,
            timeframe="1d",
            ts=opened,
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=volume,
            ts_kind="utc",
            adj_close=close,
        )
    return None


def _csv_text(payload: bytes) -> str:
    if payload[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            if not names:
                raise PermanentVendorError("bhavcopy zip contains no CSV")
            payload = archive.read(names[0])
    return payload.decode("utf-8-sig", errors="replace")


def _bind_columns(fieldnames: Sequence[str], day: date) -> dict[str, str]:
    normalized = {_header_key(name): name for name in fieldnames if name}
    bound: dict[str, str] = {}
    missing: list[str] = []
    for role, options in _COLUMNS.items():
        found = next((normalized[option] for option in options if option in normalized), None)
        if found is None:
            missing.append(role)
        else:
            bound[role] = found
    if missing:
        found = ", ".join(sorted(normalized))
        raise PermanentVendorError(
            f"bhavcopy {day.isoformat()} columns changed; missing {', '.join(missing)}; found {found}"
        )
    return bound


def _header_key(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _prices(
    row: dict[str, str | None],
    fields: dict[str, str],
) -> tuple[float, float, float, float, float] | None:
    try:
        open_ = float(row[fields["open"]] or "")
        high = float(row[fields["high"]] or "")
        low = float(row[fields["low"]] or "")
        close = float(row[fields["close"]] or "")
        volume = float(row[fields["volume"]] or "")
    except ValueError:
        return None
    return open_, high, low, close, volume
