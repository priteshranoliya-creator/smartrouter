"""Pure scoring and summary functions over stored eval results (reused by the dashboard panel)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from smartrouter.telemetry.models import EvalResult


@dataclass(frozen=True)
class ArmSummary:
    """One arm's results. Rows with an error or no judge score count in `n` and `errors`, not in the means.

    `pass_rate` is passes over scored rows. `judge_agreement` is the share of re-judged rows whose
    pass/fail matches the primary judge's, None when no row was re-judged.
    """

    arm: str
    n: int
    scored: int
    errors: int
    mean_score: float | None
    pass_rate: float | None
    total_cost_usd: float
    mean_latency_ms: float | None
    rejudged: int
    judge_agreement: float | None


def _verdict(result: EvalResult, key: str) -> dict[str, Any] | None:
    value = (result.rubric_hits or {}).get(key)
    return value if isinstance(value, dict) and isinstance(value.get("pass"), bool) else None


def passed(result: EvalResult) -> bool | None:
    """The primary judge's pass/fail, None when the row has no judgment."""
    judged = _verdict(result, "judge")
    return None if judged is None else bool(judged["pass"])


def _agreement(results: Sequence[EvalResult]) -> tuple[int, float | None]:
    matches: list[bool] = []
    for r in results:
        first, second = _verdict(r, "judge"), _verdict(r, "rejudge")
        if first is not None and second is not None:
            matches.append(first["pass"] == second["pass"])
    return len(matches), (sum(matches) / len(matches) if matches else None)


def judge_agreement(results: Iterable[EvalResult]) -> float | None:
    """Share of re-judged rows (all arms) whose pass/fail matches, None when none were re-judged."""
    return _agreement(list(results))[1]


def summarize(results: Iterable[EvalResult]) -> list[ArmSummary]:
    """Per-arm summary, arms in order of first appearance."""
    by_arm: dict[str, list[EvalResult]] = {}
    for result in results:
        by_arm.setdefault(result.arm, []).append(result)
    out: list[ArmSummary] = []
    for arm, rows in by_arm.items():
        scored = [r for r in rows if r.judge_score is not None]
        passes = [p for r in scored if (p := passed(r)) is not None]
        latencies = [r.latency_ms for r in rows if r.latency_ms is not None]
        rejudged, agreement = _agreement(rows)
        out.append(
            ArmSummary(
                arm=arm,
                n=len(rows),
                scored=len(scored),
                errors=sum(1 for r in rows if r.error is not None),
                mean_score=sum(r.judge_score or 0.0 for r in scored) / len(scored) if scored else None,
                pass_rate=sum(passes) / len(passes) if passes else None,
                total_cost_usd=sum(r.cost_usd or 0.0 for r in rows),
                mean_latency_ms=sum(latencies) / len(latencies) if latencies else None,
                rejudged=rejudged,
                judge_agreement=agreement,
            )
        )
    return out


def format_summary(summaries: Sequence[ArmSummary]) -> str:
    def pct(value: float | None) -> str:
        return "-" if value is None else f"{value:.0%}"

    width = max([len(s.arm) for s in summaries] + [len("arm")])
    head = f"{'arm':<{width}}  {'n':>4}  {'score':>5}  {'pass':>5}  {'cost $':>9}  {'latency ms':>10}  errors"
    lines = [head]
    for s in summaries:
        score = "-" if s.mean_score is None else f"{s.mean_score:.2f}"
        latency = "-" if s.mean_latency_ms is None else f"{s.mean_latency_ms:.0f}"
        lines.append(
            f"{s.arm:<{width}}  {s.n:>4}  {score:>5}  {pct(s.pass_rate):>5}  "
            f"{s.total_cost_usd:>9.4f}  {latency:>10}  {s.errors}"
        )
    rejudged = sum(s.rejudged for s in summaries)
    if rejudged:
        agree = sum(s.rejudged * (s.judge_agreement or 0.0) for s in summaries) / rejudged
        lines.append(f"judge agreement: {agree:.0%} over {rejudged} re-judged rows")
    return "\n".join(lines)
