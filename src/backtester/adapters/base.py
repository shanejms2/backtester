"""SourceAdapter interface. Adapters stay thin: vendor protocol only."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import datetime

from backtester.domain import Instrument, PairSnapshot, VendorBar
from backtester.timeframes import Timeframe


class SourceAdapter(ABC):
    """Vendor protocol. Timestamp rules and writes live in Normalize / Store."""

    source_id: str

    @abstractmethod
    def fetch_pages(
        self,
        instrument: Instrument,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        *,
        extended_hours: bool = False,
    ) -> Iterator[list[VendorBar]]:
        """Yield bounded pages of VendorBars for ``[start, end)``."""

    def resolve(self, query: str) -> Instrument | None:
        """Optional identity lookup (DexScreener). Default: not supported."""
        return None

    def fetch_snapshots(self, instrument: Instrument) -> list[PairSnapshot]:
        """Optional PairSnapshot poll. Default: none."""
        return []

    def truncated(self) -> bool:
        """True when the last fetch stopped early (history cap / compact)."""
        return False
