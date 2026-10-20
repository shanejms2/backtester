"""FastAPI application: public HTTPS fronted by nginx; binds localhost only."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backtester.api.deps import open_store
from backtester.api.routes import router
from backtester.settings import Settings, load_settings
from backtester.store.interface import BarStore


def create_app(settings: Settings | None = None, store: BarStore | None = None) -> FastAPI:
    """Build the API app. Pass ``store`` in tests to skip the lifespan client."""
    resolved = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = resolved
        if store is not None:
            app.state.store = store
            yield
            return
        gen = open_store(resolved)
        live = next(gen)
        app.state.store = live
        try:
            yield
        finally:
            with suppress(StopIteration):
                next(gen)

    app = FastAPI(
        title="Backtester API",
        version="0.1.0",
        description="Read-only JSON access to stored bars, coverage, and instruments.",
        lifespan=lifespan,
    )
    origins = _cors_origins()
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=False,
            allow_methods=["GET", "OPTIONS"],
            allow_headers=["*"],
        )
    app.include_router(router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def _cors_origins() -> list[str]:
    raw = os.environ.get("BACKTESTER_CORS_ORIGINS", "").strip()
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


app = create_app()


def run_server(
    host: str | None = None,
    port: int | None = None,
    *,
    settings: Settings | None = None,
) -> None:
    """Run uvicorn (used by ``backtester serve``)."""
    import uvicorn

    cfg = settings or load_settings()
    uvicorn.run(
        "backtester.api.app:app",
        host=host or cfg.api_host,
        port=port or cfg.api_port,
        factory=False,
        log_level="info",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI helper for ``python -m backtester.api``."""
    del argv  # reserved
    run_server()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
