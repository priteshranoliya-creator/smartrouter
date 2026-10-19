"""The decision-engine contract. Engines answer questions; the policy turns answers into a model."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from smartrouter.settings import Question


@dataclass(frozen=True)
class DecisionState:
    """What an engine sees: the (truncated) prompt plus a few short session facts."""

    prompt: str
    facts: tuple[str, ...] = ()

    def render(self) -> str:
        if not self.facts:
            return self.prompt
        return self.prompt + "\n\nSession facts:\n" + "\n".join(f"- {f}" for f in self.facts)


@dataclass(frozen=True)
class QuestionAnswer:
    """One answer: the chosen criteria key and a probability per key (summing to ~1)."""

    choice: str
    probabilities: Mapping[str, float]
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    engine: str
    answers: Mapping[str, QuestionAnswer]
    latency_ms: float


class EngineError(Exception):
    """The engine could not answer (network, model load, bad response)."""


class DecisionEngine(Protocol):
    name: str
    state_budget_tokens: int  # how much rendered state the engine reads (feature builder's budget)

    async def decide(self, state: DecisionState) -> Decision: ...


def answer_from_probabilities(
    probabilities: Mapping[str, float], raw: Mapping[str, Any] | None = None
) -> QuestionAnswer:
    choice = max(probabilities, key=lambda k: probabilities[k])
    return QuestionAnswer(choice=choice, probabilities=dict(probabilities), raw=dict(raw or {}))


class FakeEngine:
    """Deterministic engine for tests: `respond` maps a state to {question: {key: probability}}."""

    name = "fake"
    state_budget_tokens = 320

    def __init__(
        self,
        questions: Mapping[str, Question],
        respond: Callable[[DecisionState], Mapping[str, Mapping[str, float]]] | None = None,
    ) -> None:
        self._questions = questions
        self._respond = respond
        self.calls: list[DecisionState] = []

    async def decide(self, state: DecisionState) -> Decision:
        self.calls.append(state)
        if self._respond is not None:
            table = self._respond(state)
        else:  # uniform: every question equally unsure
            table = {
                name: {key: 1 / len(q.criteria) for key in q.criteria} for name, q in self._questions.items()
            }
        answers = {name: answer_from_probabilities(probs) for name, probs in table.items()}
        return Decision(engine=self.name, answers=answers, latency_ms=0.0)
