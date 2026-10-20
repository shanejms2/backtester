"""Store seam: BarStore interface and the v1 Parquet+DuckDB adapter."""

from backtester.domain import empty_bars
from backtester.store.interface import BarStore
from backtester.store.parquet_duckdb import ParquetDuckDBStore

__all__ = ["BarStore", "ParquetDuckDBStore", "empty_bars"]
