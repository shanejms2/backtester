"""guard: a risk-on mix, or a cash-heavy mix when the guard average breaks."""

from __future__ import annotations

from typing import cast

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backtester.strategy.interface import Strategy
from backtester.strategy.registry import register
from backtester.ta import sma


class GuardParams(BaseModel):
    """Two books and the average that switches them. Unknown keys fail fast.

    ``risk_on`` is the invested mix. ``risk_off`` replaces it after ``guard``
    closes below its ``exit_bars`` average; names missing from the active book
    are 0, and leftover weight is cash. The book switches back to ``risk_on``
    when ``guard`` closes above its faster ``enter_bars`` average. ``rise_bars``
    also requires that slow average to be higher than it was that many sessions
    ago. Until the exit average exists the book stays ``risk_on``.
    """

    model_config = ConfigDict(extra="forbid")

    risk_on: dict[str, float] = Field(default_factory=dict)
    risk_off: dict[str, float] = Field(default_factory=dict)
    guard: str
    exit_bars: int = 200
    enter_bars: int = 50
    rise_bars: int = 0

    @model_validator(mode="after")
    def _windows_and_books(self) -> GuardParams:
        if not self.guard.strip():
            raise ValueError("guard must name an Instrument")
        if not self.risk_on:
            raise ValueError("risk_on must list at least one Instrument")
        if self.enter_bars < 1 or self.exit_bars < 1:
            raise ValueError("averages must be at least 1 bar")
        if self.enter_bars >= self.exit_bars:
            raise ValueError("enter_bars must be < exit_bars")
        if self.rise_bars < 0:
            raise ValueError("rise_bars must be >= 0")
        return self


@register("guard")
class Guard(Strategy):
    """Hold ``risk_on`` until the guard breaks its slow average, then ``risk_off``.

    The signal uses ``adj_close`` through today's close. The Broker fills the
    new book at the next open. A faster average brings ``risk_on`` back before
    the slow average is reclaimed. With ``rise_bars`` set, that re-entry also
    waits until the slow average is higher than it was ``rise_bars`` sessions
    ago. ``ready_bars`` is 0: the slow average is not a warm-up flat, the book
    is invested until that average exists.
    """

    name = "guard"
    param_model = GuardParams

    def ready_bars(self, params: BaseModel) -> int:
        """Scored from the first bar. The exit average only gates later exits."""
        del params
        return 0

    def targets(self, panel: pl.DataFrame, params: BaseModel) -> pl.DataFrame:
        """Emit the active book's weight every bar. Missing names are 0."""
        typed = cast(GuardParams, params)
        empty = panel.select("ts_utc", "instrument_id").with_columns(pl.lit(None, dtype=pl.Float64).alias("weight"))
        if panel.height == 0:
            return empty
        px_expr = pl.coalesce(pl.col("adj_close"), pl.col("close")) if "adj_close" in panel.columns else pl.col("close")
        work = panel.with_columns(px_expr.alias("_px"))
        work = work.with_columns(
            sma(work, length=typed.exit_bars, col="_px").alias("_exit"),
            sma(work, length=typed.enter_bars, col="_px").alias("_enter"),
        )
        risk_on = _regime(work, typed)
        on_book = typed.risk_on
        off_book = typed.risk_off
        weights: list[float] = []
        for ts, instrument_id in zip(work["ts_utc"].to_list(), work["instrument_id"].to_list(), strict=True):
            book = on_book if risk_on.get(ts, True) else off_book
            weights.append(float(book.get(str(instrument_id), 0.0)))
        return work.select("ts_utc", "instrument_id").with_columns(pl.Series("weight", weights, dtype=pl.Float64))


def _regime(work: pl.DataFrame, params: GuardParams) -> dict[object, bool]:
    """True while the guard is in ``risk_on``. A missing print holds the prior state."""
    guard = work.filter(pl.col("instrument_id") == params.guard).sort("ts_utc")
    exits = guard["_exit"].to_list()
    lag = params.rise_bars
    prior = [None if lag == 0 or i < lag else exits[i - lag] for i in range(len(exits))]
    by_ts = {
        ts: (px, slow, fast, prev)
        for ts, px, slow, fast, prev in zip(
            guard["ts_utc"].to_list(),
            guard["_px"].to_list(),
            exits,
            guard["_enter"].to_list(),
            prior,
            strict=True,
        )
    }
    stamps = work.select("ts_utc").unique().sort("ts_utc").get_column("ts_utc").to_list()
    invested = True
    state: dict[object, bool] = {}
    for ts in stamps:
        row = by_ts.get(ts)
        if row is not None:
            px, slow, fast, prev = row
            rising = lag == 0 or (slow is not None and prev is not None and float(slow) > float(prev))
            if px is not None and slow is not None and invested and float(px) < float(slow):
                invested = False
            elif px is not None and fast is not None and not invested and rising and float(px) > float(fast):
                invested = True
        state[ts] = invested
    return state
