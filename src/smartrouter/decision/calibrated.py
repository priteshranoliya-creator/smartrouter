"""The calibration file and the engine wrapper that applies it to the routing question."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from smartrouter.decision.calibration import apply_temperature
from smartrouter.decision.engine import (
    Decision,
    DecisionEngine,
    DecisionState,
    answer_from_probabilities,
)

NonEmpty = Annotated[str, StringConstraints(min_length=1)]


class CalibrationError(Exception):
    """The calibration file exists but cannot be used."""


class TrainLoss(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    nll_before: float
    nll_after: float


class Calibration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    engine: NonEmpty
    question: NonEmpty
    temperature: Annotated[float, Field(gt=0)]
    n_train: Annotated[int, Field(gt=0)]
    fitted_at: NonEmpty
    train: TrainLoss
    dataset_version: NonEmpty


def write_calibration(path: Path, calibration: Calibration) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(calibration.model_dump_json(indent=2) + "\n", encoding="utf-8")


def read_calibration(path: Path) -> Calibration | None:
    """The calibration at `path`, None when there is no file, CalibrationError when it is unusable."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CalibrationError(f"{path}: cannot read file ({exc.strerror})") from None
    try:
        return Calibration.model_validate_json(text)
    except ValidationError as exc:
        raise CalibrationError(f"{path}: invalid calibration ({exc.error_count()} problems)") from None


class CalibratedEngine:
    """Rescales the routing question's probabilities and re-derives its choice; nothing else changes."""

    def __init__(self, inner: DecisionEngine, calibration: Calibration) -> None:
        self._inner = inner
        self._calibration = calibration
        self.name = inner.name
        self.state_budget_tokens = inner.state_budget_tokens

    async def decide(self, state: DecisionState) -> Decision:
        decision = await self._inner.decide(state)
        question = self._calibration.question
        answer = decision.answers.get(question)
        if answer is None:
            return decision
        scaled = apply_temperature(answer.probabilities, self._calibration.temperature)
        answers = {**decision.answers, question: answer_from_probabilities(scaled, answer.raw)}
        return replace(decision, answers=answers)
