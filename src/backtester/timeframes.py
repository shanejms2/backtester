"""Canonical Timeframe tokens and durations."""

from __future__ import annotations

from datetime import timedelta
from typing import Final, Literal, get_args

Timeframe = Literal["1m", "5m", "15m", "30m", "1h", "4h", "1d"]

TIMEFRAMES: Final[tuple[Timeframe, ...]] = get_args(Timeframe)

TIMEFRAME_DELTA: Final[dict[Timeframe, timedelta]] = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
}

# Polars `group_by_dynamic` every-strings (native tokens already match).
TIMEFRAME_POLARS: Final[dict[Timeframe, str]] = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
}


def parse_timeframe(value: str) -> Timeframe:
    """Parse a canonical Timeframe token.

    Args:
        value: Token such as ``1m`` or ``1d``.

    Returns:
        The canonical Timeframe.

    Raises:
        ValueError: If ``value`` is not a supported token.
    """
    key = value.strip().lower()
    if key in TIMEFRAME_DELTA:
        return key
    allowed = ", ".join(TIMEFRAMES)
    raise ValueError(f"unknown timeframe {value!r}; use one of {allowed}")
