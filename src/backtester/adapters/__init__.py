"""Thin vendor adapters implementing SourceAdapter."""

from backtester.adapters.alphavantage import AlphaVantageAdapter
from backtester.adapters.base import SourceAdapter
from backtester.adapters.ccxt_venue import CCXTVenueAdapter
from backtester.adapters.dexscreener import DexScreenerAdapter
from backtester.adapters.geckoterminal import GeckoTerminalAdapter
from backtester.adapters.yahoo import YahooAdapter

__all__ = [
    "AlphaVantageAdapter",
    "CCXTVenueAdapter",
    "DexScreenerAdapter",
    "GeckoTerminalAdapter",
    "SourceAdapter",
    "YahooAdapter",
]
