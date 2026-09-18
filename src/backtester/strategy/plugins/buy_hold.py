"""buy_hold: 1/N hold. Seam-prover for single-name and multi-name Universes."""

from __future__ import annotations

import polars as pl
from pydantic import BaseModel, ConfigDict

from backtester.strategy.interface import Strategy
from backtester.strategy.registry import register


class BuyHoldParams(BaseModel):
    """No knobs. Unknown keys fail fast."""

    model_config = ConfigDict(extra="forbid")


@register("buy_hold")
class BuyHold(Strategy):
    """Equal-weight long across names present at each timestamp."""

    name = "buy_hold"
    param_model = BuyHoldParams

    def ready_bars(self, params: BaseModel) -> int:
        """No Indicator warm-up."""
        return 0

    def targets(self, panel: pl.DataFrame, params: BaseModel) -> pl.DataFrame:
        """Weight ``1/N`` per timestamp (``N`` = names at that ``ts_utc``)."""
        n_at_ts = pl.col("instrument_id").n_unique().over("ts_utc")
        return panel.select(
            "ts_utc",
            "instrument_id",
            (1.0 / n_at_ts).alias("weight"),
        )
