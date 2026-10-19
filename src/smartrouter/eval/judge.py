"""LLM judge: grade an answer against a rubric, one verdict per rubric point."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from smartrouter.eval.dataset import GoldenItem
from smartrouter.eval.providers import EvalError, EvalProvider

PASS_SCORE = 0.7
ATTEMPTS = 2
JUDGE_SYSTEM_PROMPT = (
    "You are a strict grader of answers to software engineering requests. "
    "You judge only against the rubric and the reference answer. Reply with JSON only."
)
_FENCE = re.compile(r"^```[A-Za-z0-9_-]*\s*\n?(.*?)\n?```\s*$", re.DOTALL)

Verdict = Literal["hit", "miss", "wrong"]


class JudgeError(EvalError):
    """The judge never produced a usable verdict."""


class _Point(BaseModel):
    model_config = ConfigDict(extra="ignore")

    index: int
    verdict: Verdict
    reason: str = ""


class _Reply(BaseModel):
    model_config = ConfigDict(extra="ignore")

    points: list[_Point]


@dataclass(frozen=True)
class Judgment:
    model: str
    points: tuple[tuple[int, Verdict, str], ...]
    cost_usd: float
    latency_ms: float

    @property
    def score(self) -> float:
        return sum(1 for _, verdict, _ in self.points if verdict == "hit") / len(self.points)

    @property
    def passed(self) -> bool:
        return self.score >= PASS_SCORE and all(verdict != "wrong" for _, verdict, _ in self.points)

    def to_json(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "score": self.score,
            "pass": self.passed,
            "points": [{"index": i, "verdict": v, "reason": r} for i, v, r in self.points],
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
        }


def build_prompt(item: GoldenItem, answer: str) -> str:
    rubric = "\n".join(f"{n}. {point}" for n, point in enumerate(item.rubric, start=1))
    context = item.context or "(none)"
    return (
        "Grade the candidate answer against the rubric.\n\n"
        f"## Request\n{item.prompt}\n\n"
        f"## Context\n{context}\n\n"
        f"## Reference answer\n{item.reference_answer}\n\n"
        f"## Rubric points\n{rubric}\n\n"
        f"## Candidate answer\n{answer}\n\n"
        'For each rubric point give one verdict: "hit" when the candidate satisfies it, '
        '"miss" when it does not, "wrong" when the candidate states something incorrect about it. '
        "Reply with JSON only, no prose and no code fences, in exactly this shape:\n"
        '{"points": [{"index": 1, "verdict": "hit", "reason": "one short sentence"}]}\n'
        f"Include every index from 1 to {len(item.rubric)} exactly once."
    )


def parse_reply(text: str, point_count: int) -> tuple[tuple[int, Verdict, str], ...]:
    """Parse the judge's JSON, tolerating code fences and surrounding prose. Raises ValueError."""
    body = text.strip()
    fenced = _FENCE.match(body)
    if fenced:
        body = fenced.group(1).strip()
    if not body.startswith("{"):
        start, end = body.find("{"), body.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object in the reply")
        body = body[start : end + 1]
    try:
        reply = _Reply.model_validate(json.loads(body))
    except (ValueError, ValidationError) as exc:
        raise ValueError(str(exc).splitlines()[0]) from exc
    indexes = sorted(p.index for p in reply.points)
    if indexes != list(range(1, point_count + 1)):
        raise ValueError(f"expected one verdict for each index 1..{point_count}, got {indexes}")
    return tuple((p.index, p.verdict, p.reason) for p in sorted(reply.points, key=lambda p: p.index))


async def judge_answer(provider: EvalProvider, model: str, item: GoldenItem, answer: str) -> Judgment:
    """Ask `model` for a rubric verdict; retry once on unusable JSON, then raise JudgeError."""
    prompt = build_prompt(item, answer)
    cost = 0.0
    latency = 0.0
    problem = ""
    for attempt in range(ATTEMPTS):
        ask = prompt
        if attempt:
            ask += f"\n\nYour previous reply was not valid ({problem}). Reply with the JSON object only."
        reply = await provider.answer(ask, model, system=JUDGE_SYSTEM_PROMPT, json_mode=True)
        cost += reply.cost_usd
        latency += reply.latency_ms
        try:
            points = parse_reply(reply.text, len(item.rubric))
        except ValueError as exc:
            problem = str(exc)
            continue
        return Judgment(model, points, cost, latency)
    raise JudgeError(f"{model} returned invalid judge JSON after {ATTEMPTS} attempts: {problem}")


def rejudge_sample(pairs: Iterable[tuple[str, str]], fraction: float) -> frozenset[tuple[str, str]]:
    """Exactly round(fraction x n) (item, arm) pairs, at least one when fraction > 0.

    Pairs are ranked by a stable hash, so re-runs and resumes re-judge the same rows. A per-row coin
    flip drifted too far from the target on small runs (4 of 120 at 10%).
    """
    ranked = sorted(set(pairs), key=lambda p: hashlib.sha256(f"{p[0]}:{p[1]}".encode()).digest())
    if fraction <= 0 or not ranked:
        return frozenset()
    return frozenset(ranked[: max(1, round(fraction * len(ranked)))])
