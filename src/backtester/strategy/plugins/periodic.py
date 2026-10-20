"""periodic: fixed weights, reset on a calendar schedule. Drift does the holding."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, cast
from zoneinfo import ZoneInfo

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backtester.strategy.interface import Strategy
from backtester.strategy.registry import register

_NY = ZoneInfo("America/New_York")
EveryName = Literal["quarter"]


class PeriodicParams(BaseModel):
    """Fixed mix reset on schedule. Unknown keys fail fast.

    ``weights`` are the reset Targets. Other bars emit null so a drift Broker
    lets the mix follow price until the next reset. ``every: quarter`` resets
    on the last session of each calendar quarter, plus the first bar.
    """

    model_config = ConfigDict(extra="forbid")

    weights: dict[str, float] = Field(default_factory=dict)
    every: EveryName = "quarter"

    @field_validator("weights")
    @classmethod
    def _weights_ok(cls, value: dict[str, float]) -> dict[str, float]:
        if not value:
            raise ValueError("weights must list at least one Instrument")
        return dict(value)


def rebalance_stamps(stamps: list[datetime], *, every: EveryName = "quarter") -> set[datetime]:
    """First bar, plus the last bar of each calendar quarter in ``America/New_York``."""
    del every
    if not stamps:
        return set()
    local = [ts.astimezone(_NY) for ts in stamps]
    chosen = {stamps[0]}
    for i, ts in enumerate(local[:-1]):
        quarter = (ts.year, (ts.month - 1) // 3)
        nxt = local[i + 1]
        next_quarter = (nxt.year, (nxt.month - 1) // 3)
        if next_quarter != quarter:
            chosen.add(stamps[i])
    return chosen


@register("periodic")
class Periodic(Strategy):
    """Reset a fixed mix on a calendar schedule.

    The Broker fills the reset at the next open. Between resets the Target is
    null; Spec ``drift: true`` is what makes the mix follow price.
    """

    name = "periodic"
    param_model = PeriodicParams

    def ready_bars(self, params: BaseModel) -> int:
        """No Indicator warm-up."""
        del params
        return 0

    def targets(self, panel: pl.DataFrame, params: BaseModel) -> pl.DataFrame:
        """Emit reset weights on schedule. Other bars are null (hold / drift)."""
        typed = cast(PeriodicParams, params)
        if panel.height == 0:
            return panel.select("ts_utc", "instrument_id").with_columns(pl.lit(None, dtype=pl.Float64).alias("weight"))
        stamps = panel.select("ts_utc").unique().sort("ts_utc").get_column("ts_utc").to_list()
        reset = rebalance_stamps(stamps, every=typed.every)
        weights: list[float | None] = []
        for ts, instrument_id in zip(panel["ts_utc"].to_list(), panel["instrument_id"].to_list(), strict=True):
            if ts in reset:
                weights.append(float(typed.weights.get(str(instrument_id), 0.0)))
            else:
                weights.append(None)
        return panel.select("ts_utc", "instrument_id").with_columns(pl.Series("weight", weights, dtype=pl.Float64))
