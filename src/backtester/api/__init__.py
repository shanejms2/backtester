"""HTTP JSON API over BarReader / BarStore (read-only in v1)."""

from backtester.api.app import app, create_app

__all__ = ["app", "create_app"]
