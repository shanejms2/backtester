"""Vendor adapters against fixtures. No live HTTP."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from backtester.adapters.alphavantage import AlphaVantageAdapter, AlphaVantageBudget
from backtester.adapters.ccxt_venue import CCXTVenueAdapter
from backtester.adapters.dexscreener import DexScreenerAdapter
from backtester.adapters.geckoterminal import GeckoTerminalAdapter
from backtester.adapters.yahoo import YahooAdapter
from backtester.domain import Instrument
from backtester.exceptions import IncompleteHistoryError, NativeTimeframeError, PermanentVendorError, QuotaError
from backtester.normalize.bars import snapshots_to_frame
from tests.conftest import FIXTURES, aapl, btc, load_json, pepe_pool, utc


def test_yahoo_fixture_pages() -> None:
    payload = load_json("yahoo_aapl_1d.json")
    rows = payload["bars"]  # type: ignore[index]

    def download(symbol, **kwargs):  # noqa: ANN001
        del symbol, kwargs
        return rows

    adapter = YahooAdapter(download=download)
    pages = list(adapter.fetch_pages(aapl(), "1d", utc(2025, 3, 1), utc(2025, 3, 15)))
    assert len(pages) == 1
    assert pages[0][0].source == "yahoo"
    assert pages[0][0].ts.tzinfo is not None


def test_yahoo_fills_adj_close() -> None:
    rows = [
        {
            "ts": "2025-03-07T14:30:00+00:00",
            "open": 100,
            "high": 102,
            "low": 99,
            "close": 101,
            "volume": 1000,
            "adj close": 90,
        }
    ]

    def download(symbol, **kwargs):  # noqa: ANN001
        del symbol, kwargs
        return rows

    adapter = YahooAdapter(download=download)
    pages = list(adapter.fetch_pages(aapl(), "1d", utc(2025, 3, 1), utc(2025, 3, 15)))
    assert pages[0][0].adj_close == pytest.approx(90.0)


def test_yahoo_fetches_irx_as_caret_ticker() -> None:
    seen: dict[str, str] = {}

    def download(symbol, **kwargs):  # noqa: ANN001
        del kwargs
        seen["symbol"] = symbol
        return [
            {
                "ts": "2024-01-02T14:30:00+00:00",
                "open": 5.15,
                "high": 5.16,
                "low": 5.14,
                "close": 5.15,
                "volume": 0,
                "adj close": 5.15,
            }
        ]

    irx = Instrument(
        instrument_id="EQ:XNAS:IRX",
        asset_class="equity",
        venue="XNAS",
        symbol="IRX",
        session_tz="America/New_York",
        aliases=("EQ:XNAS:IRX", "IRX", "^IRX"),
    )
    adapter = YahooAdapter(download=download)
    pages = list(adapter.fetch_pages(irx, "1d", utc(2024, 1, 1), utc(2024, 1, 5)))
    assert seen["symbol"] == "^IRX"
    assert pages[0][0].vendor_symbol == "^IRX"
    assert pages[0][0].close == pytest.approx(5.15)


def test_yahoo_rejects_4h() -> None:
    adapter = YahooAdapter(download=lambda *a, **k: [])
    with pytest.raises(NativeTimeframeError, match="4h"):
        list(adapter.fetch_pages(aapl(), "4h", utc(2025, 1, 1), utc(2025, 1, 2)))


def _av_handler(fixtures: dict[str, str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        fn = params.get("function", "")
        month = params.get("month")
        if "Note" in fixtures:
            body = Path(FIXTURES / fixtures["Note"]).read_text(encoding="utf-8")
            return httpx.Response(200, text=body)
        if "Information" in fixtures:
            body = Path(FIXTURES / fixtures["Information"]).read_text(encoding="utf-8")
            return httpx.Response(200, text=body)
        if "Error" in fixtures:
            body = Path(FIXTURES / fixtures["Error"]).read_text(encoding="utf-8")
            return httpx.Response(200, text=body)
        if fn == "TIME_SERIES_DAILY":
            body = Path(FIXTURES / fixtures["daily"]).read_text(encoding="utf-8")
            return httpx.Response(200, text=body)
        if fn == "TIME_SERIES_INTRADAY":
            assert month is None or len(month) == 7
            body = Path(FIXTURES / fixtures["intraday"]).read_text(encoding="utf-8")
            return httpx.Response(200, text=body)
        return httpx.Response(404, text="{}")

    return httpx.MockTransport(handler)


def test_av_note_is_quota() -> None:
    transport = _av_handler({"Note": "av_note.json"})
    client = httpx.Client(base_url="https://www.alphavantage.co/query", transport=transport)
    adapter = AlphaVantageAdapter("demo", client=client, budget=AlphaVantageBudget(daily_limit=25))
    with pytest.raises(QuotaError, match="Note"):
        list(adapter.fetch_pages(aapl(), "1d", utc(2025, 1, 1), utc(2025, 1, 20)))


def test_av_information_is_quota() -> None:
    transport = _av_handler({"Information": "av_information.json"})
    client = httpx.Client(base_url="https://www.alphavantage.co/query", transport=transport)
    adapter = AlphaVantageAdapter("demo", client=client, budget=AlphaVantageBudget())
    with pytest.raises(QuotaError, match="Information"):
        list(adapter.fetch_pages(aapl(), "1d", utc(2025, 1, 1), utc(2025, 1, 20)))


def test_av_error_message_permanent() -> None:
    transport = _av_handler({"Error": "av_error.json"})
    client = httpx.Client(base_url="https://www.alphavantage.co/query", transport=transport)
    adapter = AlphaVantageAdapter("demo", client=client, budget=AlphaVantageBudget())
    with pytest.raises(PermanentVendorError, match="Error Message"):
        list(adapter.fetch_pages(aapl(), "1d", utc(2025, 1, 1), utc(2025, 1, 20)))


def test_av_compact_refuses_full_coverage() -> None:
    transport = _av_handler({"daily": "av_daily_compact.json"})
    client = httpx.Client(base_url="https://www.alphavantage.co/query", transport=transport)
    adapter = AlphaVantageAdapter("demo", client=client, budget=AlphaVantageBudget(), premium=False)
    with pytest.raises(IncompleteHistoryError, match="premium"):
        list(adapter.fetch_pages(aapl(), "1d", utc(2024, 1, 1), utc(2025, 1, 16)))


def test_av_month_paging() -> None:
    months: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        months.append(str(params.get("month")))
        body = (FIXTURES / "av_intraday.json").read_text(encoding="utf-8")
        return httpx.Response(200, text=body)

    client = httpx.Client(
        base_url="https://www.alphavantage.co/query",
        transport=httpx.MockTransport(handler),
    )
    adapter = AlphaVantageAdapter("demo", client=client, budget=AlphaVantageBudget())
    pages = list(adapter.fetch_pages(aapl(), "1h", utc(2025, 3, 1), utc(2025, 4, 15)))
    assert "2025-03" in months
    assert "2025-04" in months
    assert pages
    assert pages[0][0].ts_kind == "naive_session"


def test_ccxt_fixture() -> None:
    klines = load_json("ccxt_btcusdt.json")

    def fetch(symbol: str, timeframe: str, since: int | None, limit: int | None) -> list[list[float]]:
        del symbol, timeframe, since, limit
        return klines  # type: ignore[return-value]

    adapter = CCXTVenueAdapter("binance", fetch_ohlcv=fetch)
    pages = list(
        adapter.fetch_pages(
            btc(),
            "1h",
            datetime.fromtimestamp(1717200000, tz=UTC),
            datetime.fromtimestamp(1717210000, tz=UTC),
        )
    )
    assert adapter.source_id == "binance"
    assert pages[0][0].volume == pytest.approx(12.5)
    assert pages[0][0].ts.tzinfo is not None


def test_dexscreener_ticker_collision() -> None:
    payload = load_json("dexscreener_pepe.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if "/latest/dex/search" in str(request.url):
            return httpx.Response(200, json=payload)
        return httpx.Response(404, json={})

    client = httpx.Client(base_url="https://api.dexscreener.com", transport=httpx.MockTransport(handler))
    adapter = DexScreenerAdapter(client=client)
    inst = adapter.resolve("PEPE")
    assert inst.instrument_id == "DEX:solana:PepeHighLiq11111111111111111111111111111"
    assert inst.alternatives
    assert inst.alternatives[0].startswith("DEX:ethereum:")
    assert inst.pair_created_at is not None


def test_dexscreener_snapshot_not_bars() -> None:
    payload = load_json("dexscreener_pair.json")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = httpx.Client(base_url="https://api.dexscreener.com", transport=httpx.MockTransport(handler))
    adapter = DexScreenerAdapter(client=client)
    inst = pepe_pool()
    snaps = adapter.fetch_snapshots(inst)
    assert snaps[0].price_usd is not None
    frame = snapshots_to_frame(snaps)
    assert "open" not in frame.columns
    pages = list(adapter.fetch_pages(inst, "1h", utc(2024, 1, 1), utc(2024, 1, 2)))
    assert pages == []


def test_geckoterminal_ohlcv() -> None:
    payload = load_json("geckoterminal_ohlcv.json")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": []}}})

    client = httpx.Client(
        base_url="https://api.geckoterminal.com/api/v2",
        transport=httpx.MockTransport(handler),
    )
    adapter = GeckoTerminalAdapter(client=client)
    pages = list(
        adapter.fetch_pages(
            pepe_pool(),
            "1h",
            datetime.fromtimestamp(1717200000, tz=UTC),
            datetime.fromtimestamp(1717210000, tz=UTC),
        )
    )
    assert pages
    assert pages[0][0].source == "geckoterminal"
    assert pages[0][0].ts_kind == "utc"


def test_geckoterminal_401_truncates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(401, json={"error": "history"})

    client = httpx.Client(
        base_url="https://api.geckoterminal.com/api/v2",
        transport=httpx.MockTransport(handler),
    )
    adapter = GeckoTerminalAdapter(client=client)
    pages = list(adapter.fetch_pages(pepe_pool(), "1h", utc(2024, 1, 1), utc(2024, 6, 1)))
    assert pages == []
    assert adapter.truncated()


_LEGACY_BHAV = """SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN
RELIANCE,EQ,100.5,110,99,105.25,105,98,1000,50000,02-JAN-2024,10,INE002A01018
RELIANCE,BE,1,1,1,1,1,1,1,1,02-JAN-2024,1,INE002A01018
TCS,EQ,200,210,190,205,205,198,500,10000,02-JAN-2024,5,INE467B01029
"""

_UDIFF_BHAV = """TradDt,BizDt,Sgmt,TckrSymb,SctySrs,OpnPric,HghPric,LwPric,ClsPric,TtlTradgVol
2024-10-01,2024-10-01,CM,NIFTYBEES,EQ,288.78,290,287,288.27,5076492
2024-10-01,2024-10-01,CM,NIFTYBEES,BE,1,1,1,1,10
"""

_BROKEN_BHAV = """FOO,BAR
1,2
"""


def _nse(symbol: str) -> Instrument:
    return Instrument(
        instrument_id=f"EQ:XNSE:{symbol}",
        asset_class="equity",
        venue="XNSE",
        symbol=symbol,
        session_tz="Asia/Kolkata",
    )


def _zip_csv(name: str, text: str) -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, text)
    return buffer.getvalue()


def test_bhavcopy_legacy_eq_row_ignores_other_series() -> None:
    from datetime import date
    from zoneinfo import ZoneInfo

    from backtester.adapters.bhavcopy import bar_from_bhavcopy

    bar = bar_from_bhavcopy(_zip_csv("cm02JAN2024bhav.csv", _LEGACY_BHAV), "reliance", date(2024, 1, 2))
    assert bar is not None
    assert bar.source == "bhavcopy"
    assert bar.close == pytest.approx(105.25)
    assert bar.volume == pytest.approx(1000)
    assert bar.adj_close == pytest.approx(105.25)
    local = bar.ts.astimezone(ZoneInfo("Asia/Kolkata"))
    assert (local.hour, local.minute) == (9, 15)


def test_bhavcopy_udiff_columns() -> None:
    from datetime import date

    from backtester.adapters.bhavcopy import bar_from_bhavcopy

    bar = bar_from_bhavcopy(_zip_csv("BhavCopy.csv", _UDIFF_BHAV), "NIFTYBEES", date(2024, 10, 1))
    assert bar is not None
    assert bar.open == pytest.approx(288.78)
    assert bar.close == pytest.approx(288.27)


def test_bhavcopy_unknown_columns_fail_loudly() -> None:
    from datetime import date

    from backtester.adapters.bhavcopy import bar_from_bhavcopy

    with pytest.raises(PermanentVendorError, match="columns changed"):
        bar_from_bhavcopy(_BROKEN_BHAV.encode(), "RELIANCE", date(2024, 1, 2))


def test_bhavcopy_reads_saved_zip_without_http(tmp_path: Path) -> None:
    from datetime import date
    from unittest.mock import Mock

    from backtester.adapters.bhavcopy import BhavcopyAdapter, official_name

    day_dir = tmp_path / "2024" / "01"
    day_dir.mkdir(parents=True)
    name = official_name(date(2024, 1, 2))
    (day_dir / name).write_bytes(_zip_csv("cm02JAN2024bhav.csv", _LEGACY_BHAV))
    client = Mock()
    adapter = BhavcopyAdapter(tmp_path, client=client)
    pages = list(adapter.fetch_pages(_nse("RELIANCE"), "1d", utc(2024, 1, 2), utc(2024, 1, 3)))
    assert pages[0][0].close == pytest.approx(105.25)
    client.get.assert_not_called()


def test_bhavcopy_rejects_intraday(tmp_path: Path) -> None:
    from backtester.adapters.bhavcopy import BhavcopyAdapter

    adapter = BhavcopyAdapter(tmp_path)
    with pytest.raises(NativeTimeframeError):
        list(adapter.fetch_pages(_nse("RELIANCE"), "1h", utc(2024, 1, 2), utc(2024, 1, 3)))
    adapter.close()
