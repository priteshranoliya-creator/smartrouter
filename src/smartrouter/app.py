"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from smartrouter.api import hooks, proxy
from smartrouter.settings import ROOT, Settings, load_settings
from smartrouter.telemetry.events import EventBus
from smartrouter.telemetry.pricing import load_pricing
from smartrouter.telemetry.recorder import Recorder
from smartrouter.telemetry.repo import Repo


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings or load_settings()
        app.state.repo = Repo.open(app.state.settings.path("db"))
        app.state.prices = load_pricing(ROOT / "config/pricing.yaml")
        app.state.bus = EventBus()
        app.state.recorder = Recorder(app.state.repo, app.state.prices, app.state.bus)
        app.state.http = httpx.AsyncClient(
            http2=True,
            timeout=httpx.Timeout(None, connect=30.0),
            base_url=app.state.settings.router.upstream.anthropic_url,
        )
        try:
            yield
        finally:
            await app.state.http.aclose()
            app.state.repo.close()

    app = FastAPI(title="smartrouter", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(hooks.router)
    app.include_router(proxy.router)

    return app
