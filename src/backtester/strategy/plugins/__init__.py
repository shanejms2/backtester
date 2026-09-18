"""Built-in Strategy adapters. Importing this package registers them."""

from backtester.strategy.plugins.buy_hold import BuyHold, BuyHoldParams
from backtester.strategy.plugins.sma_cross import SmaCross, SmaCrossParams

__all__ = ["BuyHold", "BuyHoldParams", "SmaCross", "SmaCrossParams"]
