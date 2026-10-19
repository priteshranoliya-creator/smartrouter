"""Pure scoring and summary functions over stored eval results (reused by the dashboard panel)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from smartrouter.decision.calibration import accuracy, confusion
from smartrouter.telemetry.models import EvalResult, EvalRun


@dataclass(frozen=True)
class ArmSummary:
    """One arm's results. Rows with an error or no judge score count in `n` and `errors`, not in the means.

    `pass_rate` is passes over scored rows. `mean_cost_usd` averages the rows that report a
    cost. `judge_agreement` is the share of re-judged rows whose pass/fail matches the primary judge's,
    None when no row was re-judged.
    """

    arm: str
    n: int
    scored: int
    errors: int
    mean_score: float | None
    pass_rate: float | None
    total_cost_usd: float
    mean_cost_usd: float | None
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


@dataclass(frozen=True)
class Agreement:
    """The re-judged sample: how many rows, and the share where both judges gave the same pass/fail."""

    n: int
    rate: float | None


def agreement(results: Iterable[EvalResult]) -> Agreement:
    return Agreement(*_agreement(list(results)))


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
        costs = [r.cost_usd for r in rows if r.cost_usd is not None]
        rejudged, agreement = _agreement(rows)
        out.append(
            ArmSummary(
                arm=arm,
                n=len(rows),
                scored=len(scored),
                errors=sum(1 for r in rows if r.error is not None),
                mean_score=sum(r.judge_score or 0.0 for r in scored) / len(scored) if scored else None,
                pass_rate=sum(passes) / len(passes) if passes else None,
                total_cost_usd=sum(costs),
                mean_cost_usd=sum(costs) / len(costs) if costs else None,
                mean_latency_ms=sum(latencies) / len(latencies) if latencies else None,
                rejudged=rejudged,
                judge_agreement=agreement,
            )
        )
    return out


@dataclass(frozen=True)
class Routing:
    """Router tier predictions against the golden tiers. `confusion` is [expected][predicted].

    Every arm's rows of one item carry the same prediction, so each item counts once.
    """

    tiers: tuple[str, ...]
    n: int
    accuracy: float | None
    confusion: dict[str, dict[str, int]]


def routing(results: Iterable[EvalResult], tiers: Sequence[str]) -> Routing:
    pairs: dict[str, tuple[str, str]] = {}
    for r in results:
        expected, predicted = r.expected_tier, r.predicted_tier
        if expected in tiers and predicted in tiers:
            pairs.setdefault(r.item_id, (str(expected), str(predicted)))
    wanted = [e for e, _ in pairs.values()]
    got = [p for _, p in pairs.values()]
    return Routing(
        tiers=tuple(tiers),
        n=len(pairs),
        accuracy=accuracy(wanted, got) if pairs else None,
        confusion=confusion(wanted, got, tiers),
    )


@dataclass(frozen=True)
class QualityCostPoint:
    arm: str
    total_cost_usd: float
    mean_score: float


def quality_cost_points(summaries: Iterable[ArmSummary]) -> list[QualityCostPoint]:
    """One point per arm that has a mean score: x is its total cost, y its mean judge score."""
    return [
        QualityCostPoint(s.arm, s.total_cost_usd, s.mean_score) for s in summaries if s.mean_score is not None
    ]


@dataclass(frozen=True)
class ItemArm:
    """One arm's answer to one item. `rubric` is the stored judge output (`judge`, `rejudge`)."""

    model_used: str
    answer: str
    score: float | None
    passed: bool | None
    rubric: dict[str, Any] | None
    cost_usd: float | None
    latency_ms: float | None
    error: str | None


@dataclass(frozen=True)
class ItemRow:
    item_id: str
    expected_tier: str | None
    predicted_tier: str | None
    arms: dict[str, ItemArm]


def item_rows(results: Iterable[EvalResult], arms: Sequence[str]) -> list[ItemRow]:
    """One row per item in order of first appearance; `arms` lists the run's arms in display order."""
    by_item: dict[str, list[EvalResult]] = {}
    for r in results:
        by_item.setdefault(r.item_id, []).append(r)
    order = {arm: i for i, arm in enumerate(arms)}
    out: list[ItemRow] = []
    for item_id, rows in by_item.items():
        rows = sorted(rows, key=lambda r: order.get(r.arm, len(order)))
        out.append(
            ItemRow(
                item_id=item_id,
                expected_tier=next((r.expected_tier for r in rows if r.expected_tier), None),
                predicted_tier=next((r.predicted_tier for r in rows if r.predicted_tier), None),
                arms={
                    r.arm: ItemArm(
                        model_used=r.model_used,
                        answer=r.answer,
                        score=r.judge_score,
                        passed=passed(r),
                        rubric=r.rubric_hits,
                        cost_usd=r.cost_usd,
                        latency_ms=r.latency_ms,
                        error=r.error,
                    )
                    for r in rows
                },
            )
        )
    return out


@dataclass(frozen=True)
class RunReport:
    """Everything the CLI report and GET /api/eval/runs/{id} show for one run."""

    run: EvalRun
    arms: list[ArmSummary]
    quality_cost: list[QualityCostPoint]
    routing: Routing
    agreement: Agreement
    items: list[ItemRow]


def run_report(run: EvalRun, results: Sequence[EvalResult], tiers: Sequence[str]) -> RunReport:
    arms = summarize(results)
    by_name = {s.arm: s for s in arms}
    ordered = [by_name[a] for a in run.arms if a in by_name] + [s for s in arms if s.arm not in run.arms]
    return RunReport(
        run=run,
        arms=ordered,
        quality_cost=quality_cost_points(ordered),
        routing=routing(results, tiers),
        agreement=agreement(results),
        items=item_rows(results, run.arms),
    )


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


def _cell(value: float | None, spec: str) -> str:
    return "-" if value is None else format(value, spec)


def _table(header: Sequence[str], rows: Iterable[Sequence[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(" --- " for _ in header) + "|"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return lines


def render_run_report(report: RunReport) -> str:
    """The Markdown written to data/reports/run-N.md."""
    run = report.run
    out = [
        f"# Eval run {run.id}",
        "",
        f"Dataset `{run.dataset_version}`, judge `{run.judge_model}`, started {run.started_at}"
        + (f", finished {run.finished_at}" if run.finished_at else ", not finished")
        + ".",
        "",
        "## Arms",
        "",
        *_table(
            [
                "arm",
                "n",
                "mean score",
                "pass rate",
                "total cost $",
                "mean cost $",
                "mean latency ms",
                "errors",
            ],
            (
                [
                    s.arm,
                    str(s.n),
                    _cell(s.mean_score, ".2f"),
                    _cell(s.pass_rate, ".0%"),
                    f"{s.total_cost_usd:.4f}",
                    _cell(s.mean_cost_usd, ".4f"),
                    _cell(s.mean_latency_ms, ".0f"),
                    str(s.errors),
                ]
                for s in report.arms
            ),
        ),
        "",
        "## Quality vs cost",
        "",
        *_table(
            ["arm", "total cost $", "mean score"],
            ([p.arm, f"{p.total_cost_usd:.4f}", f"{p.mean_score:.2f}"] for p in report.quality_cost),
        ),
        "",
        "## Routing",
        "",
    ]
    r = report.routing
    if r.n == 0 or r.accuracy is None:
        out.append("No router predictions in this run.")
    else:
        out += [
            f"Routing accuracy {r.accuracy:.0%} over {r.n} items (rows: expected tier, columns: router).",
            "",
            *_table(
                ["expected \\ predicted", *r.tiers],
                ([e, *(str(r.confusion[e][p]) for p in r.tiers)] for e in r.tiers),
            ),
        ]
    a = report.agreement
    out += ["", "## Judge agreement", ""]
    out.append(
        "No rows were re-judged."
        if a.rate is None
        else f"{a.rate:.0%} pass/fail agreement over {a.n} re-judged rows."
    )
    names = [s.arm for s in report.arms]
    out += [
        "",
        "## Items",
        "",
        *_table(
            ["item", "expected", "router", *names],
            (
                [
                    i.item_id,
                    i.expected_tier or "-",
                    i.predicted_tier or "-",
                    *(
                        "-"
                        if (cell := i.arms.get(n)) is None
                        else ("error" if cell.error else _cell(cell.score, ".2f"))
                        for n in names
                    ),
                ]
                for i in report.items
            ),
        ),
        "",
    ]
    return "\n".join(out)
