"""ScaleDown `/classify` as a decision engine: one POST per question, labels and rubrics from config."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from smartrouter.decision.engine import (
    Decision,
    DecisionState,
    EngineError,
    QuestionAnswer,
    answer_from_probabilities,
)
from smartrouter.settings import Question

USER_AGENT = "smartrouter/0.1"  # the API returns 403 to the default Python User-Agent


def _label_name(criterion: str) -> str:
    return criterion.split(" - ", 1)[0].strip()


class ScaledownEngine:
    name = "scaledown"
    state_budget_tokens = 1000  # well inside /classify's text limit; keeps the full 2x1,200-char prompt

    def __init__(
        self,
        client: httpx.AsyncClient,
        url: str,
        api_key: str,
        questions: Mapping[str, Question],
        ask: Sequence[str],
        timeout_s: float,
    ) -> None:
        unknown = [name for name in ask if name not in questions]
        if unknown:
            raise EngineError(f"scaledown: unknown questions {unknown}")
        self._client = client
        self._url = url
        self._headers = {"x-api-key": api_key, "Content-Type": "application/json", "User-Agent": USER_AGENT}
        self._timeout_s = timeout_s
        self._labels: dict[str, dict[str, tuple[str, str]]] = {}
        for name in ask:
            by_key = {key: (_label_name(text), text) for key, text in questions[name].criteria.items()}
            if len({label for label, _ in by_key.values()}) != len(by_key):
                raise EngineError(f"scaledown: question {name!r} has criteria with the same label name")
            self._labels[name] = by_key

    async def decide(self, state: DecisionState) -> Decision:
        text = state.render()
        started = time.perf_counter()
        results = await asyncio.gather(*(self._classify(text, name) for name in self._labels))
        latency_ms = (time.perf_counter() - started) * 1000
        return Decision(
            engine=self.name, answers=dict(zip(self._labels, results, strict=True)), latency_ms=latency_ms
        )

    async def _classify(self, text: str, question: str) -> QuestionAnswer:
        labels = self._labels[question]
        body = {
            "text": text,
            "labels": [{"name": label, "rubric": rubric} for label, rubric in labels.values()],
        }
        try:
            response = await self._client.post(
                self._url, json=body, headers=self._headers, timeout=self._timeout_s
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise EngineError(
                f"scaledown: HTTP {exc.response.status_code} for question {question!r}"
            ) from None
        except httpx.HTTPError as exc:
            raise EngineError(f"scaledown: {type(exc).__name__} for question {question!r}") from None
        except ValueError:
            raise EngineError(f"scaledown: response for question {question!r} is not JSON") from None
        return _answer(payload, labels, question)


def _answer(payload: Any, labels: Mapping[str, tuple[str, str]], question: str) -> QuestionAnswer:
    scores = payload.get("scores") if isinstance(payload, dict) else None
    if not isinstance(scores, dict):
        raise EngineError(f"scaledown: response for question {question!r} has no scores")
    try:
        raw_scores = {key: float(scores[label]) for key, (label, _) in labels.items()}
    except (KeyError, TypeError, ValueError):
        raise EngineError(f"scaledown: scores for question {question!r} do not cover its labels") from None
    total = sum(raw_scores.values())
    if not total > 0 or any(score < 0 for score in raw_scores.values()):
        raise EngineError(f"scaledown: scores for question {question!r} are not positive")
    probabilities = {key: score / total for key, score in raw_scores.items()}
    return answer_from_probabilities(
        probabilities, {"reasoning": payload.get("reasoning"), "top_label": payload.get("top_label")}
    )
