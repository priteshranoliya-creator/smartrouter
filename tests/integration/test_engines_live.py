from __future__ import annotations

import math

import httpx
import pytest

from smartrouter.decision.engine import DecisionState
from smartrouter.decision.laya_engine import LayaEngine
from smartrouter.settings import load_settings

STATE = DecisionState(
    "Refactor the session cache to avoid a race between eviction and refill across workers."
)


@pytest.mark.laya
async def test_real_laya_engine_answers_complexity() -> None:
    questions = load_settings().questions
    engine = LayaEngine(questions, "english", "cpu")
    await engine.decide(STATE)  # warm-up
    decision = await engine.decide(STATE)
    answer = decision.answers["complexity"]
    assert set(answer.probabilities) == set("ABCD")
    assert math.isclose(sum(answer.probabilities.values()), 1, abs_tol=1e-3)
    assert decision.latency_ms > 0
    print(f"laya english cpu latency_ms={decision.latency_ms:.0f} choice={answer.choice}")


@pytest.mark.scaledown
async def test_real_scaledown_api_answers_complexity() -> None:
    from smartrouter.decision.factory import build_engine

    settings = load_settings()
    if settings.secrets.scaledown_api_key is None:
        pytest.skip("SCALEDOWN_API_KEY not set")
    async with httpx.AsyncClient() as http:
        decision = await build_engine(settings, http).decide(STATE)
    answer = decision.answers["complexity"]
    assert set(answer.probabilities) == set("ABCD")
    assert math.isclose(sum(answer.probabilities.values()), 1, abs_tol=1e-6)
    assert decision.latency_ms > 0
    print(f"scaledown latency_ms={decision.latency_ms:.0f} choice={answer.choice}")
