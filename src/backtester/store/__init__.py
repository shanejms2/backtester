"""Store seam: BarStore interface and the v1 Parquet+DuckDB adapter."""

from backtester.store.interface import BarStore
from backtester.store.parquet_duckdb import ParquetDuckDBStore, empty_bars

__all__ = ["BarStore", "ParquetDuckDBStore", "empty_bars"]
