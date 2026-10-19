"""Laya (local ModernBERT classifier) as a decision engine. `laya` is an optional extra, imported lazily."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Mapping
from typing import Any

from smartrouter.decision.engine import (
    Decision,
    DecisionState,
    EngineError,
    QuestionAnswer,
    answer_from_probabilities,
)
from smartrouter.settings import Question


def _load_router(checkpoint: str, device: str) -> Any:
    os.environ.setdefault("USE_TF", "0")  # transformers otherwise probes TensorFlow, which can hang on load
    try:
        from laya import Router
    except ImportError:
        raise EngineError("laya engine needs the extra: uv sync --extra laya") from None
    router = Router(device=device, max_loaded=1)
    router.preload([checkpoint])
    return router


class LayaEngine:
    state_budget_tokens = 320  # the English checkpoint reads 512 tokens, ~320 left for the state

    def __init__(
        self,
        questions: Mapping[str, Question],
        checkpoint: str,
        device: str,
        router: Any | None = None,
    ) -> None:
        self.name = f"laya:{checkpoint}"
        self._questions = {name: question.model_dump() for name, question in questions.items()}
        self._checkpoint = checkpoint
        self._router = router if router is not None else _load_router(checkpoint, device)

    async def decide(self, state: DecisionState) -> Decision:
        started = time.perf_counter()
        try:
            out = await asyncio.to_thread(
                self._router.predict, {"request": state.render()}, self._questions, model=self._checkpoint
            )
            answers = {name: _answer(name, raw) for name, raw in out["answers"].items()}
        except EngineError:
            raise
        except Exception as exc:
            raise EngineError(f"laya: {type(exc).__name__}: {exc}") from exc
        return Decision(engine=self.name, answers=answers, latency_ms=(time.perf_counter() - started) * 1000)


def _answer(question: str, raw: Mapping[str, Any]) -> QuestionAnswer:
    probabilities = raw.get("probabilities")
    if not isinstance(probabilities, Mapping) or not probabilities:
        raise EngineError(f"laya: answer for question {question!r} has no probabilities")
    # Never gate on Laya's own `confidence` (docs/spikes/laya.md, Finding 1); it is kept for the log only.
    extras = {"confidence": raw.get("confidence"), "answer_confidence": raw.get("answer_confidence")}
    return answer_from_probabilities({key: float(p) for key, p in probabilities.items()}, extras)
