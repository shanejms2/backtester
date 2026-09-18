"""Strategy engine: interface, registry, Broker, Spec, Compare."""

from backtester.strategy.engine import (
    compare_spec,
    format_metrics_table,
    run_spec,
    run_yaml,
    write_equity_chart,
)
from backtester.strategy.interface import Strategy
from backtester.strategy.registry import get, list_strategies, load_plugin, register
from backtester.strategy.spec import Spec, load_spec
from backtester.strategy.types import BrokerConfig, Comparison, Fill, Metrics, RunResult

__all__ = [
    "BrokerConfig",
    "Comparison",
    "Fill",
    "Metrics",
    "RunResult",
    "Spec",
    "Strategy",
    "compare_spec",
    "format_metrics_table",
    "get",
    "list_strategies",
    "load_plugin",
    "load_spec",
    "register",
    "run_spec",
    "run_yaml",
    "write_equity_chart",
]
