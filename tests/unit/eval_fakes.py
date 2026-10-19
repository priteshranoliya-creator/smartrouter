"""Offline stand-ins for eval providers."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable

from smartrouter.eval.dataset import GoldenItem
from smartrouter.eval.judge import JUDGE_SYSTEM_PROMPT
from smartrouter.eval.providers import EvalAnswer, EvalError
from smartrouter.providers.base import Usage

COST = {"claude-opus-5-5": 0.03, "claude-sonnet-5-5": 0.01, "claude-haiku-5-5": 0.002}


def item(n: int, tier: str, marker: str, split: str = "test") -> GoldenItem:
    return GoldenItem.model_validate(
        {
            "id": f"g-{n:04d}",
            "prompt": f"{marker} request",
            "context": "",
            "reference_answer": "ref",
            "rubric": ["point one", "point two"],
            "expected_tier": tier,
            "task_type": "edit",
            "split": split,
            "tags": [],
        }
    )


def all_hit(prompt: str, model: str) -> str:
    return json.dumps({"points": [{"index": i, "verdict": "hit", "reason": "ok"} for i in (1, 2)]})


class FakeProvider:
    """Answers `answer:<model>`; a judge call (judge system prompt) goes to `judge_reply`."""

    def __init__(
        self,
        judge_reply: Callable[[str, str], str] = all_hit,
        fail_models: set[str] | None = None,
    ) -> None:
        self.judge_reply = judge_reply
        self.fail_models = fail_models or set()
        self.answer_calls: Counter[tuple[str, str]] = Counter()
        self.judge_calls: Counter[tuple[str, str]] = Counter()

    async def answer(
        self, prompt: str, model: str, *, system: str | None = None, json_mode: bool = False
    ) -> EvalAnswer:
        if system == JUDGE_SYSTEM_PROMPT:
            self.judge_calls[(prompt, model)] += 1
            return EvalAnswer(self.judge_reply(prompt, model), model, Usage(), 0.001, 5.0)
        if model in self.fail_models:
            raise EvalError(f"{model} is down")
        self.answer_calls[(prompt, model)] += 1
        return EvalAnswer(f"answer:{model}", model, Usage(), COST.get(model, 0.005), 100.0)
