"""Built-in Strategy adapters. Importing this package registers them."""

from backtester.strategy.plugins.buy_hold import BuyHold, BuyHoldParams
from backtester.strategy.plugins.cap_mix import CapMix, CapMixParams
from backtester.strategy.plugins.guard import Guard, GuardParams
from backtester.strategy.plugins.periodic import Periodic, PeriodicParams
from backtester.strategy.plugins.prism import Prism, PrismParams
from backtester.strategy.plugins.sma_cross import SmaCross, SmaCrossParams
from backtester.strategy.plugins.static import Static, StaticParams
from backtester.strategy.plugins.year_winner import YearWinner, YearWinnerParams

__all__ = [
    "BuyHold",
    "BuyHoldParams",
    "CapMix",
    "CapMixParams",
    "Guard",
    "GuardParams",
    "Periodic",
    "PeriodicParams",
    "Prism",
    "PrismParams",
    "SmaCross",
    "SmaCrossParams",
    "Static",
    "StaticParams",
    "YearWinner",
    "YearWinnerParams",
]
