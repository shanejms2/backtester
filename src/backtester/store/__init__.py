"""Store seam: BarStore interface and the ClickHouse adapter."""

from backtester.domain import empty_bars
from backtester.store.clickhouse import ClickHouseStore
from backtester.store.interface import BarStore

__all__ = ["BarStore", "ClickHouseStore", "empty_bars"]
