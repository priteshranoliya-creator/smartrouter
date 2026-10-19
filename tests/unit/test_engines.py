from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import yaml

from smartrouter.decision.engine import DecisionState, EngineError, FakeEngine
from smartrouter.decision.factory import build_engine
from smartrouter.decision.laya_engine import LayaEngine
from smartrouter.decision.scaledown_engine import ScaledownEngine
from smartrouter.settings import ROOT, Question, load_settings

URL = "https://scaledown.test/classify"
STATE = DecisionState("rename foo to bar", ("turn 2 of this session",))
SETTINGS = load_settings(env={"SCALEDOWN_API_KEY": "sd-secret"})
QUESTIONS = SETTINGS.questions


def scores_payload(**scores: float) -> dict[str, Any]:
    return {"top_label": max(scores, key=lambda k: scores[k]), "scores": scores, "reasoning": "because"}


async def run_scaledown(*, ask: list[str] | None = None, timeout_s: float = 5) -> Any:
    async with httpx.AsyncClient() as client:
        engine = ScaledownEngine(client, URL, "sd-secret", QUESTIONS, ask or ["complexity"], timeout_s)
        return await engine.decide(STATE)


async def test_fake_engine_defaults_to_uniform_answers() -> None:
    engine = FakeEngine(QUESTIONS)
    decision = await engine.decide(STATE)
    assert decision.engine == "fake"
    assert set(decision.answers) == set(QUESTIONS)
    assert decision.answers["complexity"].probabilities == dict.fromkeys("ABCD", 0.25)
    assert engine.calls == [STATE]


async def test_fake_engine_custom_responder() -> None:
    engine = FakeEngine(QUESTIONS, lambda state: {"complexity": {"A": 0.1, "D": 0.9}})
    answer = (await engine.decide(STATE)).answers["complexity"]
    assert answer.choice == "D"
    assert answer.probabilities == {"A": 0.1, "D": 0.9}


@respx.mock
async def test_scaledown_sends_labels_and_rubrics_from_the_question() -> None:
    route = respx.post(URL).respond(json=scores_payload(trivial=0.1, standard=0.5, involved=0.3, hard=0.1))
    await run_scaledown()
    request = route.calls.last.request
    body = json.loads(request.content)
    criteria = QUESTIONS["complexity"].criteria
    assert body["text"] == STATE.render()
    assert body["labels"] == [
        {"name": "trivial", "rubric": criteria["A"]},
        {"name": "standard", "rubric": criteria["B"]},
        {"name": "involved", "rubric": criteria["C"]},
        {"name": "hard", "rubric": criteria["D"]},
    ]
    assert request.headers["x-api-key"] == "sd-secret"
    assert request.headers["content-type"] == "application/json"
    agent = request.headers["user-agent"]
    assert agent and not agent.startswith(("Python-urllib", "python-httpx", "python-requests"))


@respx.mock
async def test_scaledown_maps_scores_back_to_keys_and_normalises() -> None:
    respx.post(URL).respond(json=scores_payload(trivial=0.2, standard=0.2, involved=0.2, hard=0.8))
    decision = await run_scaledown()
    answer = decision.answers["complexity"]
    assert decision.engine == "scaledown"
    assert answer.choice == "D"
    assert answer.probabilities == pytest.approx({"A": 1 / 7, "B": 1 / 7, "C": 1 / 7, "D": 4 / 7})
    assert math.isclose(sum(answer.probabilities.values()), 1)
    assert answer.raw == {"reasoning": "because", "top_label": "hard"}
    assert decision.latency_ms > 0


@respx.mock
async def test_scaledown_asks_each_listed_question_once() -> None:
    route = respx.post(URL).mock(
        side_effect=lambda request: httpx.Response(
            200,
            json=scores_payload(**{label["name"]: 1.0 for label in json.loads(request.content)["labels"]}),
        )
    )
    decision = await run_scaledown(ask=["complexity", "continuation"])
    assert route.call_count == 2
    assert set(decision.answers) == {"complexity", "continuation"}
    assert set(decision.answers["continuation"].probabilities) == {"A", "B"}


@respx.mock
@pytest.mark.parametrize("status", [403, 500])
async def test_scaledown_http_errors_raise_engine_error(status: int) -> None:
    respx.post(URL).respond(status)
    with pytest.raises(EngineError, match=str(status)):
        await run_scaledown()


@respx.mock
async def test_scaledown_timeout_raises_engine_error() -> None:
    respx.post(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(EngineError, match="ReadTimeout"):
        await run_scaledown()


@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"<html>not json"),
        httpx.Response(200, json=["a list"]),
        httpx.Response(200, json={"top_label": "hard"}),
        httpx.Response(200, json=scores_payload(trivial=1.0, hard=1.0)),
        httpx.Response(200, json=scores_payload(trivial=0, standard=0, involved=0, hard=0)),
        httpx.Response(200, json={"scores": {"trivial": "x", "standard": 0, "involved": 0, "hard": 0}}),
    ],
    ids=["not-json", "list", "no-scores", "missing-labels", "all-zero", "non-numeric"],
)
async def test_scaledown_malformed_response_raises_engine_error(response: httpx.Response) -> None:
    respx.post(URL).mock(return_value=response)
    with pytest.raises(EngineError):
        await run_scaledown()


def test_scaledown_rejects_unknown_question_and_duplicate_label_names() -> None:
    client = httpx.AsyncClient()
    with pytest.raises(EngineError, match="unknown"):
        ScaledownEngine(client, URL, "k", QUESTIONS, ["nope"], 1)
    clash = {"q": Question(type="choice", instructions="?", criteria={"A": "same - one", "B": "same - two"})}
    with pytest.raises(EngineError, match="same label"):
        ScaledownEngine(client, URL, "k", clash, ["q"], 1)


class FakeRouter:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def predict(self, state: dict[str, str], questions: dict[str, Any], model: str) -> dict[str, Any]:
        self.calls.append((state, questions, model))
        return {
            "answers": {
                "complexity": {
                    "choice": "B",
                    "confidence": 0.1,
                    "answer_confidence": 0.45,
                    "probabilities": {"A": 0.2, "B": 0.45, "C": 0.3, "D": 0.05},
                }
            }
        }


async def test_laya_engine_calls_router_with_checkpoint_and_maps_probabilities() -> None:
    router = FakeRouter()
    engine = LayaEngine(QUESTIONS, "english", "cpu", router=router)
    decision = await engine.decide(STATE)
    state, questions, model = router.calls[0]
    assert model == "english"
    assert state == {"request": STATE.render()}
    assert questions["complexity"]["criteria"] == dict(QUESTIONS["complexity"].criteria)
    assert decision.engine == "laya:english"
    answer = decision.answers["complexity"]
    assert answer.choice == "B"
    assert answer.probabilities == {"A": 0.2, "B": 0.45, "C": 0.3, "D": 0.05}
    assert answer.raw == {"confidence": 0.1, "answer_confidence": 0.45}
    assert decision.latency_ms > 0


async def test_laya_engine_wraps_router_failures() -> None:
    class Broken:
        def predict(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("boom")

    with pytest.raises(EngineError, match="boom"):
        await LayaEngine(QUESTIONS, "english", "cpu", router=Broken()).decide(STATE)

    class NoProbabilities:
        def predict(self, *args: Any, **kwargs: Any) -> Any:
            return {"answers": {"complexity": {"choice": "A"}}}

    with pytest.raises(EngineError, match="probabilities"):
        await LayaEngine(QUESTIONS, "english", "cpu", router=NoProbabilities()).decide(STATE)


def settings_with_engine(tmp_path: Path, engine: str, key: str | None) -> Any:
    router = yaml.safe_load((ROOT / "config/router.yaml").read_text())
    router["decision"]["engine"] = engine
    router["paths"]["calibration"] = str(tmp_path / "calibration.json")
    (tmp_path / "router.yaml").write_text(yaml.safe_dump(router))
    (tmp_path / "questions.yaml").write_text((ROOT / "config/questions.yaml").read_text())
    return load_settings(tmp_path, env={"SCALEDOWN_API_KEY": key} if key else {})


async def test_factory_picks_the_configured_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "smartrouter.decision.laya_engine._load_router", lambda checkpoint, device: FakeRouter()
    )
    async with httpx.AsyncClient() as http:
        assert isinstance(
            build_engine(settings_with_engine(tmp_path, "scaledown", "k"), http), ScaledownEngine
        )
        assert isinstance(build_engine(settings_with_engine(tmp_path, "laya", None), http), LayaEngine)
        assert isinstance(build_engine(settings_with_engine(tmp_path, "fake", None), http), FakeEngine)


@respx.mock
async def test_factory_scaledown_asks_only_the_routing_question(tmp_path: Path) -> None:
    route = respx.post("https://api.scaledown.xyz/classify").respond(
        json=scores_payload(trivial=0.1, standard=0.5, involved=0.3, hard=0.1)
    )
    async with httpx.AsyncClient() as http:
        engine = build_engine(settings_with_engine(tmp_path, "scaledown", "sd-key"), http)
        decision = await engine.decide(STATE)
    assert route.call_count == 1
    assert route.calls.last.request.headers["x-api-key"] == "sd-key"
    assert set(decision.answers) == {"complexity"}


async def test_factory_fails_clearly_without_a_scaledown_key(tmp_path: Path) -> None:
    async with httpx.AsyncClient() as http:
        with pytest.raises(EngineError, match="SCALEDOWN_API_KEY"):
            build_engine(settings_with_engine(tmp_path, "scaledown", None), http)
