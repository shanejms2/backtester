"""sma_cross: fast/slow SMA cross via ``ta.sma``. Signed Targets when long_short."""

from __future__ import annotations

from typing import Literal, cast

import polars as pl
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from backtester.strategy.interface import Strategy
from backtester.strategy.registry import register
from backtester.ta import sma

Side = Literal["long_only", "long_short"]


class SmaCrossParams(BaseModel):
    """Fast/slow windows and side. Unknown keys fail fast."""

    model_config = ConfigDict(extra="forbid")

    fast: int = 20
    slow: int = 50
    side: Side = "long_only"

    @field_validator("fast", "slow")
    @classmethod
    def _positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("SMA length must be >= 1")
        return value

    @model_validator(mode="after")
    def _fast_lt_slow(self) -> SmaCrossParams:
        if self.fast >= self.slow:
            raise ValueError("fast must be < slow")
        return self


@register("sma_cross")
class SmaCross(Strategy):
    """Per-name SMA cross, scaled to ``1/N`` so gross is at most 1."""

    name = "sma_cross"
    param_model = SmaCrossParams

    def ready_bars(self, params: BaseModel) -> int:
        """Warm-up is the slow SMA period."""
        typed = cast(SmaCrossParams, params)
        return typed.slow

    def targets(self, panel: pl.DataFrame, params: BaseModel) -> pl.DataFrame:
        """Long when fast > slow; short (or flat) when fast < slow.

        Calls ``ta.sma`` twice. Null SMA (warm-up) yields null weight.
        """
        typed = cast(SmaCrossParams, params)
        work = panel.with_columns(
            sma(panel, length=typed.fast).alias("sma_fast"),
            sma(panel, length=typed.slow).alias("sma_slow"),
        )
        n_at_ts = pl.col("instrument_id").n_unique().over("ts_utc")
        unit = 1.0 / n_at_ts
        long_sig = pl.col("sma_fast") > pl.col("sma_slow")
        short_sig = pl.col("sma_fast") < pl.col("sma_slow")
        if typed.side == "long_short":
            signed = pl.when(long_sig).then(unit).when(short_sig).then(-unit).otherwise(0.0)
        else:
            signed = pl.when(long_sig).then(unit).otherwise(0.0)
        warm = pl.col("sma_fast").is_null() | pl.col("sma_slow").is_null()
        weight = pl.when(warm).then(None).otherwise(signed)
        return work.select("ts_utc", "instrument_id", weight.alias("weight"))
