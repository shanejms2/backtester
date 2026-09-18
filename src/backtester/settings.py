"""Process settings from the environment. Never print secrets."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, SecretStr


def _load_dotenv() -> None:
    """Load ``.env`` from the cwd if present. Missing file is fine."""
    load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)


class Settings(BaseModel):
    """Runtime configuration. API keys stay in ``SecretStr`` and are never logged."""

    model_config = ConfigDict(frozen=True)

    data_dir: Path = Field(default_factory=lambda: Path("data"))
    alphavantage_api_key: SecretStr | None = None
    display_tz: str = "UTC"
    hot_1m_days: int = 30
    av_premium: bool = False


def load_settings() -> Settings:
    """Build Settings from env after loading ``.env``.

    Returns:
        Frozen settings. ``ALPHAVANTAGE_API_KEY`` is wrapped, not echoed.
    """
    _load_dotenv()
    raw_key = os.environ.get("ALPHAVANTAGE_API_KEY", "").strip()
    tz = os.environ.get("BACKTESTER_TZ", "").strip() or "UTC"
    data_dir = Path(os.environ.get("BACKTESTER_DATA_DIR", "data"))
    hot = int(os.environ.get("BACKTESTER_HOT_1M_DAYS", "30"))
    premium = os.environ.get("BACKTESTER_AV_PREMIUM", "").strip() in {"1", "true", "True", "yes"}
    return Settings(
        data_dir=data_dir,
        alphavantage_api_key=SecretStr(raw_key) if raw_key else None,
        display_tz=tz,
        hot_1m_days=hot,
        av_premium=premium,
    )
