"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from smartrouter.settings import Settings, load_settings
from smartrouter.telemetry.repo import Repo


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings or load_settings()
        app.state.repo = Repo.open(app.state.settings.path("db"))
        try:
            yield
        finally:
            app.state.repo.close()

    app = FastAPI(title="smartrouter", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
