"""API key gate for public HTTPS. Health stays open."""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status

from backtester.settings import Settings


def require_api_key(
    settings: Settings,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """Reject when ``BACKTESTER_API_KEY`` is set and the header does not match.

    When the key is empty (local dev), the check is skipped. Public HTTPS
    deploys must set a non-empty key in ``.env``.
    """
    expected = settings.api_key
    if expected is None:
        return
    provided = (x_api_key or "").strip()
    if not provided or not secrets.compare_digest(provided, expected.get_secret_value()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing X-API-Key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
