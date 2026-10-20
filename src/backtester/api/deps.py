"""FastAPI dependencies: settings, store, auth."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Header, Request

from backtester.api.auth import require_api_key
from backtester.settings import Settings, load_settings
from backtester.store.clickhouse import ClickHouseStore
from backtester.store.interface import BarStore


def get_settings(request: Request) -> Settings:
    settings = getattr(request.app.state, "settings", None)
    if isinstance(settings, Settings):
        return settings
    return load_settings()


def get_store(request: Request) -> BarStore:
    store = getattr(request.app.state, "store", None)
    if store is None:
        raise RuntimeError("ClickHouse store is not attached to the app")
    return store  # type: ignore[no-any-return]


def open_store(settings: Settings) -> Iterator[ClickHouseStore]:
    store = ClickHouseStore(
        host=settings.clickhouse_host,
        port=settings.clickhouse_port,
        username=settings.clickhouse_user,
        password=settings.clickhouse_password.get_secret_value(),
        database=settings.clickhouse_database,
    )
    try:
        yield store
    finally:
        store.close()


def enforce_api_key(
    settings: Annotated[Settings, Depends(get_settings)],
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    require_api_key(settings, x_api_key)


SettingsDep = Annotated[Settings, Depends(get_settings)]
StoreDep = Annotated[BarStore, Depends(get_store)]
