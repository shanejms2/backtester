"""year_winner: hold the best full prior calendar years, reset each year-end."""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, date, datetime
from typing import Literal, cast
from zoneinfo import ZoneInfo

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backtester import ta
from backtester.calendar.session import SessionCalendar
from backtester.strategy.interface import Strategy
from backtester.strategy.registry import register

_IST = ZoneInfo("Asia/Kolkata")
EveryName = Literal["year", "quarter"]


class YearWinnerParams(BaseModel):
    """Score from ``trade_from``. The Panel must already contain the prior year.

    Unknown keys fail fast. A name is eligible for a period when its first
    print is in the period's first month, it prints on the last session, and
    it has at least ``min_prints`` sessions. ``every`` is ``year`` or
    ``quarter``. A quarter still resets four times a year, but the score is
    the trailing year into that quarter-end: the name must print on the
    quarter-end a year earlier and have ``min_prints`` sessions in between
    (use 150). The book is the ``n`` highest ``adj_close`` returns, equal
    weight. Ties break on instrument id. Fewer than ``n`` eligible names
    share the book equally. ``stop_bars`` sells a held name, between
    period-ends, when its close is under that average, and leaves the sleeve
    in cash until the next period-end.
    """

    model_config = ConfigDict(extra="forbid")

    history_from: date
    trade_from: date
    min_prints: int = Field(default=150, ge=2)
    n: int = Field(default=1, ge=1)
    every: EveryName = "year"
    stop_bars: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _order(self) -> YearWinnerParams:
        if self.trade_from <= self.history_from:
            raise ValueError("trade_from must be after history_from")
        return self


@register("year_winner")
class YearWinner(Strategy):
    """Each year, or each quarter on a trailing year, buy the best prior returns.

    A year scores that calendar year. A quarter scores the year ending on that
    quarter's last session, then holds the book until the next quarter-end.
    The Broker fills the signal at the next open. Other sessions are null.
    ``n`` is how many names share the book, equal weight. ``ready_bars``
    skips the history window so Metrics start on ``trade_from``.
    """

    name = "year_winner"
    param_model = YearWinnerParams

    def ready_bars(self, params: BaseModel) -> int:
        """NSE sessions in ``[history_from, trade_from)``."""
        typed = cast(YearWinnerParams, params)
        start = datetime(typed.history_from.year, typed.history_from.month, typed.history_from.day, tzinfo=UTC)
        end = datetime(typed.trade_from.year, typed.trade_from.month, typed.trade_from.day, tzinfo=UTC)
        return len(SessionCalendar().xnse_session_dates(start, end))

    def targets(self, panel: pl.DataFrame, params: BaseModel) -> pl.DataFrame:
        """Emit equal weight on the top ``n`` names at each period-end. Other bars are null."""
        typed = cast(YearWinnerParams, params)
        empty = panel.select("ts_utc", "instrument_id").with_columns(pl.lit(None, dtype=pl.Float64).alias("weight"))
        if panel.height == 0:
            return empty
        px_expr = pl.coalesce(pl.col("adj_close"), pl.col("close")) if "adj_close" in panel.columns else pl.col("close")
        work = panel.with_columns(px_expr.alias("_px"))
        winners = _period_winners(work, min_prints=typed.min_prints, n=typed.n, every=typed.every)
        if typed.stop_bars == 0:
            weights = _book_weights(work, winners)
        else:
            weights = _book_weights_with_stop(work, winners, stop_bars=typed.stop_bars)
        return work.select("ts_utc", "instrument_id").with_columns(pl.Series("weight", weights, dtype=pl.Float64))


def _book_weights(work: pl.DataFrame, winners: dict[datetime, tuple[str, ...]]) -> list[float | None]:
    """Equal weight on the chosen names at each period-end. Other bars are null."""
    weights: list[float | None] = []
    for ts, instrument_id in zip(work["ts_utc"].to_list(), work["instrument_id"].to_list(), strict=True):
        chosen = winners.get(ts)
        if not chosen:
            weights.append(None)
        else:
            share = 1.0 / len(chosen)
            weights.append(share if str(instrument_id) in chosen else 0.0)
    return weights


def _book_weights_with_stop(
    work: pl.DataFrame,
    winners: dict[datetime, tuple[str, ...]],
    *,
    stop_bars: int,
) -> list[float | None]:
    """Period-end book, then a close under the average sells that name until the next reset."""
    averages = ta.sma(work, length=stop_bars, col="close").to_list()
    stamps = work["ts_utc"].to_list()
    ids = [str(item) for item in work["instrument_id"].to_list()]
    closes = work["close"].to_list()
    rows_at: dict[datetime, list[int]] = {}
    for index, ts in enumerate(stamps):
        rows_at.setdefault(ts, []).append(index)
    weights: list[float | None] = [None] * len(stamps)
    active: set[str] = set()
    stopped: set[str] = set()
    for ts in sorted(rows_at):
        chosen = winners.get(ts)
        if chosen is not None:
            stopped = set()
            active = set(chosen)
            share = 1.0 / len(chosen)
            for index in rows_at[ts]:
                weights[index] = share if ids[index] in active else 0.0
            continue
        exiting: list[str] = []
        for index in rows_at[ts]:
            iid = ids[index]
            if iid not in active or iid in stopped:
                continue
            price = closes[index]
            average = averages[index]
            if price is None or average is None:
                continue
            if float(price) < float(average):
                exiting.append(iid)
        stopped.update(exiting)
        for index in rows_at[ts]:
            if ids[index] in exiting:
                weights[index] = 0.0
    return weights


def _period_winners(
    work: pl.DataFrame, *, min_prints: int, n: int, every: EveryName
) -> dict[datetime, tuple[str, ...]]:
    """Map each period-end stamp to the ``n`` instruments with the best full-period returns."""
    stamps = work.select("ts_utc").unique().sort("ts_utc").get_column("ts_utc").to_list()
    if len(stamps) < 2:
        return {}
    ist_dates = [ts.astimezone(_IST).date() for ts in stamps]
    buckets = [_bucket(day, every) for day in ist_dates]
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
    if every == "quarter":
        return _rolling_year_winners(period_ends, by_id, min_prints=min_prints, n=n)
    winners: dict[datetime, tuple[str, ...]] = {}
    for ts in period_ends:
        end = ts.astimezone(_IST).date()
        key = _bucket(end, every)
        start_month = 1 if every == "year" else key[1] * 3 + 1
        ranked: list[tuple[float, str]] = []
        for instrument_id, rows in by_id.items():
            span = [row for row in rows if _bucket(row[0], every) == key]
            if not _full_span(span, end, min_prints=min_prints, start_month=start_month):
                continue
            ret = span[-1][1] / span[0][1] - 1.0
            ranked.append((ret, instrument_id))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        chosen = tuple(instrument_id for _, instrument_id in ranked[:n])
        if chosen:
            winners[ts] = chosen
    return winners


def _rolling_year_winners(
    period_ends: list[datetime],
    by_id: dict[str, list[tuple[date, float]]],
    *,
    min_prints: int,
    n: int,
    ids: Collection[str] | None = None,
) -> dict[datetime, tuple[str, ...]]:
    """Rank a quarter by the year ending on that quarter's last session."""
    end_on = {ts.astimezone(_IST).date(): ts for ts in period_ends}
    by_quarter = {_bucket(day, "quarter"): day for day in end_on}
    winners: dict[datetime, tuple[str, ...]] = {}
    for end, ts in end_on.items():
        anchor = by_quarter.get((end.year - 1, (end.month - 1) // 3))
        if anchor is None:
            continue
        ranked: list[tuple[float, str]] = []
        names = by_id.keys() if ids is None else ids
        for instrument_id in names:
            rows = by_id.get(instrument_id)
            if rows is None:
                continue
            prices = dict(rows)
            start_px = prices.get(anchor)
            end_px = prices.get(end)
            if start_px is None or end_px is None or start_px <= 0:
                continue
            prints = sum(1 for day, _ in rows if anchor <= day <= end)
            if prints < min_prints:
                continue
            ranked.append((end_px / start_px - 1.0, instrument_id))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        chosen = tuple(instrument_id for _, instrument_id in ranked[:n])
        if chosen:
            winners[ts] = chosen
    return winners


def _bucket(day: date, every: EveryName) -> tuple[int, int]:
    """Calendar year, or calendar quarter, in IST session dates."""
    if every == "year":
        return (day.year, 0)
    return (day.year, (day.month - 1) // 3)


def _full_span(rows: list[tuple[date, float]], end: date, *, min_prints: int, start_month: int) -> bool:
    """First print in the period's first month, a print on the last session, enough sessions."""
    if len(rows) < min_prints:
        return False
    if rows[0][0].month != start_month or rows[0][1] <= 0:
        return False
    return rows[-1][0] == end
