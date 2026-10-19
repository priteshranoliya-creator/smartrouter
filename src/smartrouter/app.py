"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from smartrouter.api import dashboard, decide, hooks, proxy, stream
from smartrouter.decision.engine import DecisionEngine, EngineError
from smartrouter.decision.factory import build_engine
from smartrouter.decision.service import DecisionService
from smartrouter.settings import ROOT, ConfigError, Mode, Settings, load_settings
from smartrouter.telemetry.events import EventBus
from smartrouter.telemetry.pricing import load_pricing
from smartrouter.telemetry.recorder import Recorder
from smartrouter.telemetry.repo import Repo

DASHBOARD_DIR = ROOT / "dashboard"


def create_app(settings: Settings | None = None, engine: DecisionEngine | None = None) -> FastAPI:
    """`engine` replaces the configured decision engine (tests inject one)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings or load_settings()
        engine_http = httpx.AsyncClient()
        mode = app.state.settings.router.mode
        try:
            active_engine = engine or (
                None if mode is Mode.OFF else build_engine(app.state.settings, engine_http)
            )
        except EngineError as exc:
            await engine_http.aclose()
            raise ConfigError(str(exc)) from None
        app.state.engine = active_engine
        app.state.engine_http = engine_http
        app.state.mode = mode.value
        app.state.upstream_probe = None
        app.state.repo = Repo.open(app.state.settings.path("db"))
        app.state.prices = load_pricing(ROOT / "config/pricing.yaml")
        app.state.bus = EventBus()
        app.state.decisions = (
            None
            if active_engine is None or mode is Mode.OFF
            else DecisionService(active_engine, app.state.settings, app.state.prices, app.state.repo)
        )
        app.state.recorder = Recorder(app.state.repo, app.state.prices, app.state.bus, app.state.decisions)
        app.state.http = httpx.AsyncClient(
            http2=True,
            timeout=httpx.Timeout(None, connect=30.0),
            base_url=app.state.settings.router.upstream.anthropic_url,
        )
        try:
            yield
        finally:
            if app.state.decisions is not None:
                await app.state.decisions.aclose()
            await engine_http.aclose()
            await app.state.http.aclose()
            app.state.repo.close()

    app = FastAPI(title="smartrouter", lifespan=lifespan)

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(DASHBOARD_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=DASHBOARD_DIR), name="static")

    app.include_router(dashboard.router)
    app.include_router(stream.router)
    app.include_router(decide.router)
    app.include_router(hooks.router)
    app.include_router(proxy.router)

    return app
