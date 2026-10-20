"""Causal Polars technical analysis on a Panel.

Indicators return columns, never weights. Rolling windows are grouped by
``instrument_id``. At bar ``t`` an Indicator may use that bar's OHLCV and
history through ``t`` — never bar ``t+1``. Warm-up is null, except EMA / Wilder
averages which document an SMA seed.

This module is not a Strategy and not a fill model.
"""

from __future__ import annotations

from backtester.ta.momentum import cci, mom, roc, rsi, stoch, stoch_rsi, willr
from backtester.ta.structure import swing_high, swing_labels, swing_low, swings
from backtester.ta.trend import adx, aroon, ema, macd, sma, supertrend, wma
from backtester.ta.volatility import atr, bbands, donchian, ewm_std, keltner, stdev
from backtester.ta.volume import cmf, mfi, obv, vwap

CATALOG: tuple[str, ...] = (
    "sma",
    "ema",
    "wma",
    "macd",
    "adx",
    "supertrend",
    "aroon",
    "rsi",
    "stoch",
    "stoch_rsi",
    "cci",
    "willr",
    "roc",
    "mom",
    "atr",
    "bbands",
    "keltner",
    "donchian",
    "stdev",
    "ewm_std",
    "obv",
    "mfi",
    "cmf",
    "vwap",
    "swing_high",
    "swing_low",
    "swing_labels",
    "swings",
)


def available() -> list[str]:
    """Return catalog names in stable order (trend, momentum, volatility, volume, structure)."""
    return list(CATALOG)


__all__ = [
    "CATALOG",
    "adx",
    "aroon",
    "atr",
    "available",
    "bbands",
    "cci",
    "cmf",
    "donchian",
    "ema",
    "ewm_std",
    "keltner",
    "macd",
    "mfi",
    "mom",
    "obv",
    "roc",
    "rsi",
    "sma",
    "stdev",
    "stoch",
    "stoch_rsi",
    "supertrend",
    "swing_high",
    "swing_labels",
    "swing_low",
    "swings",
    "vwap",
    "willr",
    "wma",
]
