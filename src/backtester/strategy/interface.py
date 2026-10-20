"""Strategy interface: the plugin seam (ADR-0005)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

import polars as pl
from pydantic import BaseModel


class Strategy(ABC):
    """Causal function of a Panel through this bar's close.

    Output is a Target frame — columns ``ts_utc``, ``instrument_id``, ``weight`` —
    never a Fill. Cross-section math stays in the plugin. The engine talks only
    to this interface plus the registry.
    """

    name: ClassVar[str]
    param_model: ClassVar[type[BaseModel]]

    @abstractmethod
    def ready_bars(self, params: BaseModel) -> int:
        """Return warm-up bars so Compare can align at first ready timestamp.

        Args:
            params: Validated ``param_model`` instance.

        Returns:
            Bars to skip at the start of the Metrics window (at least the max
            Indicator period this Strategy uses).
        """

    @abstractmethod
    def targets(self, panel: pl.DataFrame, params: BaseModel) -> pl.DataFrame:
        """Emit signed Target weights from the Panel through this close.

        Args:
            panel: Long-format Bars plus per-name metadata. May be used through
                this row's close; must not read bar ``t+1``.
            params: Validated ``param_model`` instance.

        Returns:
            Frame with ``ts_utc``, ``instrument_id``, ``weight``. Null weight
            means ineligible: the Broker holds the last weight. Weight ``0``
            is a full exit.
        """
