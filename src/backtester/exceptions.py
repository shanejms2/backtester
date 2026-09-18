"""Public exceptions for the market data engine."""

from __future__ import annotations


class BacktesterError(Exception):
    """Base error for the market data engine."""


class InvalidTimezoneError(BacktesterError):
    """Display timezone is an abbreviation, fixed offset, or unknown IANA name."""


class UnknownInstrumentError(BacktesterError):
    """Catalog cannot resolve a query to an Instrument."""


class VendorError(BacktesterError):
    """Vendor protocol failed."""


class QuotaError(VendorError):
    """Retryable vendor quota / rate-limit (HTTP 429 or Alpha Vantage Note JSON)."""


class PermanentVendorError(VendorError):
    """Non-retryable vendor error (unknown symbol, bad request)."""


class IncompleteHistoryError(VendorError):
    """Vendor returned a truncated series; Coverage must not be marked complete."""


class NativeTimeframeError(BacktesterError):
    """Vendor cannot supply the requested native Timeframe."""


class InvalidBarError(BacktesterError):
    """A Bar failed OHLC / finiteness / non-future checks."""
