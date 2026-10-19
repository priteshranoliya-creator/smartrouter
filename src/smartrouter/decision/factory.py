"""Choose the decision engine from settings."""

from __future__ import annotations

import logging

import httpx

from smartrouter.decision.calibrated import CalibratedEngine, CalibrationError, read_calibration
from smartrouter.decision.engine import DecisionEngine, EngineError, FakeEngine
from smartrouter.decision.laya_engine import LayaEngine
from smartrouter.decision.scaledown_engine import ScaledownEngine
from smartrouter.settings import Engine, Settings

logger = logging.getLogger(__name__)


def build_engine(settings: Settings, http: httpx.AsyncClient) -> DecisionEngine:
    """The configured engine, wrapped in its calibration when a matching calibration file exists."""
    engine = build_raw_engine(settings, http)
    path = settings.path("calibration")
    try:
        calibration = read_calibration(path)
    except CalibrationError as exc:
        logger.warning("calibration skipped: %s", exc)
        return engine
    if calibration is None:
        return engine
    question = settings.router.decision.question
    if calibration.engine != engine.name or calibration.question != question:
        logger.warning(
            "calibration skipped: %s was fitted for engine %r question %r, configured is %r question %r",
            path,
            calibration.engine,
            calibration.question,
            engine.name,
            question,
        )
        return engine
    logger.info("calibration applied: %s, temperature %.3f", engine.name, calibration.temperature)
    return CalibratedEngine(engine, calibration)


def build_raw_engine(settings: Settings, http: httpx.AsyncClient) -> DecisionEngine:
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
