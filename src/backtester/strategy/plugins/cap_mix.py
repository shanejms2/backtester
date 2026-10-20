"""cap_mix: each quarter, the best trailing year from each size bucket."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import cast
from zoneinfo import ZoneInfo

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backtester.calendar.session import SessionCalendar
from backtester.strategy.interface import Strategy
from backtester.strategy.plugins.year_winner import _bucket, _rolling_year_winners
from backtester.strategy.registry import register

_IST = ZoneInfo("Asia/Kolkata")


class CapSleeve(BaseModel):
    """How many names to take from one list of instrument ids.

    Unknown keys fail fast. ``ids`` are the only names allowed in this sleeve.
    """

    model_config = ConfigDict(extra="forbid")

    n: int = Field(ge=1)
    ids: tuple[str, ...] = Field(min_length=1)


class CapMixParams(BaseModel):
    """Quarterly trailing-year picks, one ranking per sleeve.

    A name is eligible when it prints on the quarter-end a year earlier and
    has at least ``min_prints`` sessions in that year. Each sleeve keeps its
    own top ``n``. The chosen names share the book equally. A sleeve with no
    eligible name is left in cash.
    """

    model_config = ConfigDict(extra="forbid")

    history_from: date
    trade_from: date
    min_prints: int = Field(default=150, ge=2)
    sleeves: tuple[CapSleeve, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _order(self) -> CapMixParams:
        if self.trade_from <= self.history_from:
            raise ValueError("trade_from must be after history_from")
        return self


@register("cap_mix")
class CapMix(Strategy):
    """Each quarter, buy the trailing-year leaders of every sleeve.

    The signal is the last NSE session of the quarter. The Broker fills it at
    the next open. Other sessions are null, so the holding stays put until
    the next quarter-end. ``ready_bars`` skips the history window so Metrics
    start on ``trade_from``.
    """

    name = "cap_mix"
    param_model = CapMixParams

    def ready_bars(self, params: BaseModel) -> int:
        """NSE sessions in ``[history_from, trade_from)``."""
        typed = cast(CapMixParams, params)
        start = datetime(typed.history_from.year, typed.history_from.month, typed.history_from.day, tzinfo=UTC)
        end = datetime(typed.trade_from.year, typed.trade_from.month, typed.trade_from.day, tzinfo=UTC)
        return len(SessionCalendar().xnse_session_dates(start, end))

    def targets(self, panel: pl.DataFrame, params: BaseModel) -> pl.DataFrame:
        """Emit equal weight on each sleeve's leaders at the quarter-end."""
        typed = cast(CapMixParams, params)
        empty = panel.select("ts_utc", "instrument_id").with_columns(pl.lit(None, dtype=pl.Float64).alias("weight"))
        if panel.height == 0:
            return empty
        px_expr = pl.coalesce(pl.col("adj_close"), pl.col("close")) if "adj_close" in panel.columns else pl.col("close")
        work = panel.with_columns(px_expr.alias("_px"))
        winners = _sleeve_winners(work, typed)
        weights: list[float | None] = []
        for ts, instrument_id in zip(work["ts_utc"].to_list(), work["instrument_id"].to_list(), strict=True):
            chosen = winners.get(ts)
            if not chosen:
                weights.append(None)
            else:
                share = 1.0 / len(chosen)
                weights.append(share if str(instrument_id) in chosen else 0.0)
        return work.select("ts_utc", "instrument_id").with_columns(pl.Series("weight", weights, dtype=pl.Float64))


def _sleeve_winners(work: pl.DataFrame, params: CapMixParams) -> dict[datetime, tuple[str, ...]]:
    """Combine each sleeve's trailing-year leaders at the same quarter-end."""
    stamps = work.select("ts_utc").unique().sort("ts_utc").get_column("ts_utc").to_list()
    if len(stamps) < 2:
        return {}
    ist_dates = [ts.astimezone(_IST).date() for ts in stamps]
    buckets = [_bucket(day, "quarter") for day in ist_dates]
    period_ends = [stamps[i] for i in range(len(stamps) - 1) if buckets[i] != buckets[i + 1]]
    by_id: dict[str, list[tuple[date, float]]] = {}
    for ts, instrument_id, px in zip(
        work["ts_utc"].to_list(),
        work["instrument_id"].to_list(),
        work["_px"].to_list(),
        strict=True,
    ):
        if px is None:
            continue
        by_id.setdefault(str(instrument_id), []).append((ts.astimezone(_IST).date(), float(px)))
    for rows in by_id.values():
        rows.sort()
    merged: dict[datetime, list[str]] = {}
    for sleeve in params.sleeves:
        picked = _rolling_year_winners(
            period_ends,
            by_id,
            min_prints=params.min_prints,
            n=sleeve.n,
            ids=sleeve.ids,
        )
        for ts, names in picked.items():
            merged.setdefault(ts, []).extend(names)
    return {ts: tuple(names) for ts, names in merged.items() if names}
