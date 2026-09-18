"""BarStore interface: the Store seam. Ingest must not import Parquet paths."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import datetime

import polars as pl

from backtester.domain import CoverageInterval, Instrument, WriteResult
from backtester.timeframes import Timeframe


class BarStore(ABC):
    """Persistence seam for Bars, Coverage, snapshots, and Instrument records.

    v1 implementation is Parquet + DuckDB. A later Postgres adapter sits here;
    one implementation is a hypothetical seam, two would make it real.
    """

    @abstractmethod
    def write_batch(self, bars: pl.DataFrame) -> WriteResult:
        """Dedup, stage, validate, atomically publish a page of Bars."""

    @abstractmethod
    def write_snapshots(self, snapshots: pl.DataFrame) -> WriteResult:
        """Persist PairSnapshots (not Bars)."""

    @abstractmethod
    def scan(
        self,
        instrument_id: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        *,
        columns: Sequence[str] | None = None,
        source: str | None = None,
    ) -> pl.LazyFrame:
        """Lazy scan of matching Hive partitions. Predicate-pushes year/month."""

    @abstractmethod
    def coverage(
        self,
        instrument_id: str,
        timeframe: Timeframe,
        source: str,
    ) -> list[CoverageInterval]:
        """Inclusive UTC ranges already fetched for this triple."""

    @abstractmethod
    def update_coverage(
        self,
        instrument_id: str,
        timeframe: Timeframe,
        source: str,
        start: datetime,
        end: datetime,
    ) -> None:
        """Union ``[start, end]`` into Coverage (inclusive)."""

    @abstractmethod
    def drop_coverage(
        self,
        instrument_id: str,
        timeframe: Timeframe,
        source: str,
        start: datetime,
        end: datetime,
    ) -> None:
        """Subtract a UTC range from Coverage (retention / prune)."""

    @abstractmethod
    def compact(self, instrument_id: str | None = None) -> int:
        """Merge tiny parts in a partition. Returns files removed."""

    @abstractmethod
    def prune_hot_1m(self, *, hot_days: int, now: datetime | None = None) -> int:
        """Drop 1m files older than ``hot_days`` when a native 1h or 1d series exists."""

    @abstractmethod
    def upsert_instrument(self, instrument: Instrument) -> None:
        """Insert or replace a Catalog record."""

    @abstractmethod
    def get_instrument(self, instrument_id: str) -> Instrument | None:
        """Load one Instrument from the catalog tables."""

    @abstractmethod
    def find_by_alias(self, alias: str) -> Instrument | None:
        """Lookup by alias (case-insensitive)."""

    @abstractmethod
    def last_snapshot_poll(self, instrument_id: str, source: str) -> datetime | None:
        """Last successful PairSnapshot poll (not a bar range)."""
