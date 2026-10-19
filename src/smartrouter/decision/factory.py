"""Choose the decision engine from settings."""

from __future__ import annotations

import httpx

from smartrouter.decision.engine import DecisionEngine, EngineError, FakeEngine
from smartrouter.decision.laya_engine import LayaEngine
from smartrouter.decision.scaledown_engine import ScaledownEngine
from smartrouter.settings import Engine, Settings


def build_engine(settings: Settings, http: httpx.AsyncClient) -> DecisionEngine:
    decision = settings.router.decision
    match decision.engine:
        case Engine.SCALEDOWN:
            key = settings.secrets.scaledown_api_key
            if key is None:
                raise EngineError(
                    "decision.engine is scaledown but SCALEDOWN_API_KEY is not set (put it in .env)"
                )
            scaledown = decision.engines.scaledown
            return ScaledownEngine(
                http,
                scaledown.url,
                key.get_secret_value(),
                settings.questions,
                ask=[decision.question],
                timeout_s=scaledown.timeout_s,
            )
        case Engine.LAYA:
            laya = decision.engines.laya
            return LayaEngine(settings.questions, laya.checkpoint, laya.device.value)
        case Engine.FAKE:
            return FakeEngine(settings.questions)
