"""YAML Spec: Universe + Runs that share one Panel."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from backtester.exceptions import InvalidSpecError
from backtester.timeframes import Timeframe, parse_timeframe

AssetClassName = Literal["equity", "crypto", "dex"]
ClockName = Literal["native", "nyse_1600", "xnse"]
RoleName = Literal["index_etf", "commodity_etf", "crypto", "stock", "inverse_etf"]


def _parse_bound(value: datetime | date | str, *, is_end: bool) -> datetime:
    """Coerce a Spec date/datetime to aware UTC.

    Date-only end bounds are exclusive (start of the next UTC day), matching
    the CLI ingest/read convention.
    """
    date_only = False
    if isinstance(value, datetime):
        parsed = value
        date_only = parsed.hour == 0 and parsed.minute == 0 and parsed.second == 0 and parsed.microsecond == 0
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day, tzinfo=UTC)
        date_only = True
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        date_only = len(text) == 10
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise InvalidSpecError(f"invalid datetime {value!r}") from exc
    parsed = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    if date_only and is_end:
        parsed = parsed + timedelta(days=1)
    return parsed


class UniverseRow(BaseModel):
    """One Instrument in the Spec Universe, with per-name metadata.

    Attributes:
        id: Catalog instrument id. The engine never branches on ticker.
        class_: ``equity`` / ``crypto`` / ``dex`` (YAML key ``class``).
        calendar: ``native_24_7`` or ``nyse`` / ``XNYS`` / ``us_equity``.
        fee_bps: Per-class fee on traded notional (Broker).
        min_adv: Optional eligibility hint for plugins (Broker ignores).
        source: Vendor override; defaults to Spec ``source``.
        role: Optional sleeve label (``index_etf``, ``commodity_etf``,
            ``crypto``, ``stock``, ``inverse_etf``). The engine does not branch
            on it; PRISM haircuts ``stock`` and ``inverse_etf``.
        currency: ISO code. Non-USD names are converted via Spec ``fx``.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    id: str
    class_: AssetClassName = Field(alias="class")
    calendar: str = "native_24_7"
    fee_bps: float = 0.0
    min_adv: float | None = None
    source: str | None = None
    role: RoleName | None = None
    currency: str = "USD"


class RunSpec(BaseModel):
    """One Strategy + params on the shared Panel.

    ``drift`` overrides Spec ``drift`` for this Run only. ``None`` inherits
    the Spec, so a quarterly book can follow price while another Run on the
    same Panel stays constant-mix.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    strategy: str
    params: dict[str, Any] = Field(default_factory=dict)
    drift: bool | None = None


class Spec(BaseModel):
    """YAML list of Runs that share Universe, Timeframe, window, and costs.

    ``clock: nyse_1600`` builds a decision Panel on NYSE session days only.
    ``clock: xnse`` does the same on NSE session days (09:15–15:30 IST).
    ``total_return: true`` scales each bar's OHLC by ``adj_close / close`` so
    splits and dividends stay in the Broker's price path.
    ``cash_rate_id`` is a T-bill *yield* series (Yahoo IRX close is an
    annualized percent, e.g. ``5.15`` = 5.15%). It is not a Universe name
    and is never clustered. Daily cash return is ``close / 100 / 365``.
    Flat ``rebalance_band`` remains the default for ``sma_cross``;
    ``band_floor`` / ``band_frac`` / ``half_move`` select the proportional path.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    name: str
    timeframe: Timeframe
    from_: datetime = Field(alias="from")
    source: str
    universe: list[UniverseRow]
    runs: list[RunSpec]
    to: datetime | None = None
    rebalance_band: float = 0.0
    band_floor: float = 0.0
    band_frac: float = 0.0
    half_move: bool = False
    allow_short: bool = True
    drift: bool = False
    initial_equity: float = 1.0
    plugins: list[str] = Field(default_factory=list)
    clock: ClockName = "native"
    total_return: bool = False
    cash_rate_id: str | None = None
    fx: dict[str, str] = Field(default_factory=dict)

    @field_validator("timeframe", mode="before")
    @classmethod
    def _timeframe(cls, value: object) -> Timeframe:
        if not isinstance(value, str):
            raise TypeError("timeframe must be a string")
        return parse_timeframe(value)

    @field_validator("from_", mode="before")
    @classmethod
    def _from(cls, value: object) -> datetime:
        if not isinstance(value, datetime | date | str):
            raise TypeError("from must be a date or datetime")
        return _parse_bound(value, is_end=False)

    @field_validator("to", mode="before")
    @classmethod
    def _to(cls, value: object) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime | date | str):
            raise TypeError("to must be a date or datetime")
        return _parse_bound(value, is_end=True)

    @field_validator("universe")
    @classmethod
    def _universe_nonempty(cls, value: list[UniverseRow]) -> list[UniverseRow]:
        if not value:
            raise ValueError("universe must list at least one Instrument")
        return value

    @field_validator("runs")
    @classmethod
    def _runs_nonempty(cls, value: list[RunSpec]) -> list[RunSpec]:
        if not value:
            raise ValueError("runs must list at least one Run")
        return value

    @property
    def end(self) -> datetime:
        """Exclusive-ish window end (``to``, or now UTC)."""
        return self.to if self.to is not None else datetime.now(UTC)


def load_spec(path: Path | str) -> Spec:
    """Parse a YAML Spec file.

    Args:
        path: Filesystem path to the Spec.

    Returns:
        Validated Spec.

    Raises:
        InvalidSpecError: Missing file, non-mapping YAML, or pydantic failure.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise InvalidSpecError(f"Spec not found: {file_path}")
    raw = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise InvalidSpecError("Spec must be a YAML mapping")
    try:
        return Spec.model_validate(raw)
    except ValidationError as exc:
        raise InvalidSpecError(str(exc)) from exc
