"""static: constant Target weights from ``params.weights``."""

from __future__ import annotations

from typing import cast

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from backtester.strategy.interface import Strategy
from backtester.strategy.registry import register


class StaticParams(BaseModel):
    """Map of ``instrument_id`` → weight. Unknown keys fail fast."""

    model_config = ConfigDict(extra="forbid")

    weights: dict[str, float] = Field(default_factory=dict)


@register("static")
class Static(Strategy):
    """Hold the configured weights on every bar.

    Names present on the Panel but missing from ``weights`` get 0. Used for
    single-name buy-and-hold (e.g. 100% SPY) on a shared multi-name Panel.
    """

    name = "static"
    param_model = StaticParams

    def ready_bars(self, params: BaseModel) -> int:
        """No Indicator warm-up."""
        return 0

    def targets(self, panel: pl.DataFrame, params: BaseModel) -> pl.DataFrame:
        """Emit the configured weight for each ``instrument_id``."""
        typed = cast(StaticParams, params)
        mapping = typed.weights
        if not mapping:
            weight: pl.Expr = pl.lit(0.0)
        else:
            weight = pl.col("instrument_id").replace_strict(mapping, default=0.0)
        return panel.select("ts_utc", "instrument_id", weight.alias("weight"))
