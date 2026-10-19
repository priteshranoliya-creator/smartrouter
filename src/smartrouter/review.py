"""T6.1 go/no-go review: shadow-mode data and the latest eval run against the TDD section 1 targets.

Pure functions over stored rows; `router review` renders the result as Markdown.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from smartrouter.eval.report import ArmSummary, Routing
from smartrouter.settings import ReviewConfig
from smartrouter.telemetry.models import DecisionFact, RequestRow
from smartrouter.telemetry.stats import percentile, request_costs

Status = Literal["pass", "fail", "no data"]


@dataclass(frozen=True)
class Check:
    goal: str
    metric: str
    target: str
    value: str
    status: Status
    note: str = ""


@dataclass(frozen=True)
class Coverage:
    sessions: int
    turns: int
    days: float

    def enough(self, targets: ReviewConfig) -> bool:
        return self.days >= targets.min_shadow_days and self.turns >= targets.min_shadow_turns


@dataclass(frozen=True)
class Review:
    coverage: Coverage
    checks: tuple[Check, ...]
    eval_run_id: int | None

    @property
    def go(self) -> bool:
        return bool(self.checks) and all(c.status == "pass" for c in self.checks)


def _status(value: float | None, ok: bool) -> Status:
    return "no data" if value is None else ("pass" if ok else "fail")


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def coverage(facts: Sequence[DecisionFact]) -> Coverage:
    shadow = [f for f in facts if f.decision.mode == "shadow"]
    if not shadow:
        return Coverage(sessions=0, turns=0, days=0.0)
    times = sorted(datetime.fromisoformat(f.turn.created_at) for f in shadow)
    return Coverage(
        sessions=len({f.turn.session_id for f in shadow}),
        turns=len(shadow),
        days=(times[-1] - times[0]).total_seconds() / 86400,
    )


def cost_checks(summaries: Sequence[ArmSummary], targets: ReviewConfig) -> list[Check]:
    """Router vs the baseline arm on the golden set: cost cut and quality kept."""
    by_arm = {s.arm: s for s in summaries}
    router, base = by_arm.get("router"), by_arm.get(targets.baseline_arm)
    reduction = quality = None
    if router and base and base.total_cost_usd > 0:
        reduction = 1 - router.total_cost_usd / base.total_cost_usd
    if router and base and router.mean_score is not None and base.mean_score:
        quality = router.mean_score / base.mean_score
    detail = (
        f"router ${router.total_cost_usd:.2f} vs {base.arm} ${base.total_cost_usd:.2f}"
        if router and base
        else ""
    )
    return [
        Check(
            "Cut cost",
            f"golden-set cost vs all-{targets.baseline_arm}",
            f">= {targets.min_cost_reduction:.0%} lower",
            _pct(reduction),
            _status(reduction, reduction is not None and reduction >= targets.min_cost_reduction),
            detail,
        ),
        Check(
            "Keep quality",
            f"judged quality vs all-{targets.baseline_arm}",
            f">= {targets.min_quality_ratio:.0%}",
            _pct(quality),
            _status(quality, quality is not None and quality >= targets.min_quality_ratio),
        ),
    ]


def switch_paybacks(facts: Sequence[DecisionFact], rows: Sequence[RequestRow]) -> list[bool]:
    """One entry per router switch: did the saving until the session's next switch cover its tax?

    A switch is a turn whose chosen model differs from the router's previous choice in the session
    (the first decision compares with the model the turn asked for). Turn savings come from the logged
    counterfactual costs, which price the cold cache of the first request on the new model. A switch
    whose turns have no counterfactual logged is left out: there is nothing to judge it on.
    """
    saving: dict[int, float] = defaultdict(float)
    for row in rows:
        if row.request.turn_id is not None and row.request.counterfactual_cost_usd is not None:
            without, with_router = request_costs(row)
            saving[row.request.turn_id] += without - with_router
    sessions: dict[str, list[DecisionFact]] = defaultdict(list)
    for fact in facts:
        sessions[fact.turn.session_id].append(fact)
    paid: list[bool] = []
    for session in sessions.values():
        session.sort(key=lambda f: f.turn.seq)
        segment: list[float] | None = None
        previous = session[0].first_main_requested_model
        for fact in session:
            chosen = fact.decision.chosen_model
            if previous is not None and chosen != previous:
                if segment:
                    paid.append(sum(segment) >= 0)
                segment = []
            if segment is not None and fact.turn.id in saving:
                segment.append(saving[fact.turn.id])
            previous = chosen
        if segment:
            paid.append(sum(segment) >= 0)
    return paid


def payback_check(facts: Sequence[DecisionFact], rows: Sequence[RequestRow], targets: ReviewConfig) -> Check:
    paid = switch_paybacks(facts, rows)
    share = sum(paid) / len(paid) if paid else None
    return Check(
        "Switches pay back",
        "switches whose tax was recovered in the session",
        f">= {targets.min_payback_share:.0%}",
        _pct(share),
        _status(share, share is not None and share >= targets.min_payback_share),
        f"{sum(paid)} of {len(paid)} switches" if paid else "no switches yet",
    )


def added_latencies(facts: Sequence[DecisionFact], rows: Sequence[RequestRow]) -> list[float]:
    """Per decided turn: how long the proxy held its first main request before forwarding it.

    The decision never holds a request (tdd-changes #28), so this is the router's own overhead,
    measured the same way in shadow and active mode. Requests logged before T6.2 have none.
    """
    first_main: dict[int, float | None] = {}
    for row in rows:
        req = row.request
        if req.kind == "main" and req.turn_id is not None and req.turn_id not in first_main:
            first_main[req.turn_id] = req.overhead_ms
    waits = [first_main.get(f.turn.id) for f in facts if f.turn.id is not None]
    return [w for w in waits if w is not None]


def latency_check(facts: Sequence[DecisionFact], rows: Sequence[RequestRow], targets: ReviewConfig) -> Check:
    waits = added_latencies(facts, rows)
    p50, p95 = percentile(waits, 0.5), percentile(waits, 0.95)
    return Check(
        "Stay fast",
        "added latency per prompt, p50",
        f"< {targets.max_added_latency_p50_ms:.0f} ms",
        "-" if p50 is None else f"{p50:.0f} ms",
        _status(p50, p50 is not None and p50 < targets.max_added_latency_p50_ms),
        "" if p95 is None else f"p95 {p95:.0f} ms over {len(waits)} turns",
    )


def visibility_checks(
    facts: Sequence[DecisionFact], rows: Sequence[RequestRow], targets: ReviewConfig
) -> list[Check]:
    """Requests with usage and cost logged (an upstream error with its message counts as logged), and
    main turns with a decision row."""
    logged = [
        (r.request.input_tokens is not None and r.request.output_tokens is not None)
        or (r.request.status is not None and r.request.status >= 400 and bool(r.request.error))
        for r in rows
    ]
    request_share = sum(logged) / len(logged) if logged else None
    main_turns = {
        r.request.turn_id for r in rows if r.request.kind == "main" and r.request.turn_id is not None
    }
    decided = {f.turn.id for f in facts}
    decision_share = len(main_turns & decided) / len(main_turns) if main_turns else None
    return [
        Check(
            "Full visibility",
            "proxied requests with usage and cost",
            f">= {targets.min_logged_share:.0%}",
            _pct(request_share),
            _status(request_share, request_share is not None and request_share >= targets.min_logged_share),
            f"{sum(logged)} of {len(logged)}" if logged else "",
        ),
        Check(
            "Full visibility",
            "main turns with a decision row",
            f">= {targets.min_logged_share:.0%}",
            _pct(decision_share),
            _status(
                decision_share, decision_share is not None and decision_share >= targets.min_logged_share
            ),
            f"{len(main_turns & decided)} of {len(main_turns)}" if main_turns else "",
        ),
    ]


def routing_check(routing: Routing) -> Check:
    """Tier accuracy on the test split against always predicting its most common tier."""
    counts = {tier: sum(row.values()) for tier, row in routing.confusion.items()}
    baseline = max(counts.values()) / routing.n if routing.n else None
    accuracy = routing.accuracy
    ok = accuracy is not None and baseline is not None and accuracy > baseline
    return Check(
        "Trustworthy routing",
        "tier accuracy on the golden test split",
        "> majority-class baseline" + ("" if baseline is None else f" ({baseline:.0%})"),
        _pct(accuracy),
        _status(accuracy, ok),
        f"{routing.n} items",
    )


def review(
    *,
    facts: Sequence[DecisionFact],
    rows: Sequence[RequestRow],
    summaries: Sequence[ArmSummary],
    routing: Routing | None,
    eval_run_id: int | None,
    targets: ReviewConfig,
) -> Review:
    checks = [
        *cost_checks(summaries, targets),
        payback_check(facts, rows, targets),
        latency_check(facts, rows, targets),
        *visibility_checks(facts, rows, targets),
    ]
    if routing is not None:
        checks.append(routing_check(routing))
    return Review(coverage=coverage(facts), checks=tuple(checks), eval_run_id=eval_run_id)


def render(result: Review, targets: ReviewConfig) -> str:
    cov = result.coverage
    enough = cov.enough(targets)
    verdict = ("GO" if result.go else "NO-GO") if enough else "NO-GO (not enough shadow data yet)"
    lines = [
        "# Go/no-go review (T6.1)",
        "",
        f"**Verdict: {verdict}**",
        "",
        f"Shadow data: {cov.turns} turns in {cov.sessions} sessions over {cov.days:.1f} days "
        f"(needs {targets.min_shadow_turns} turns over {targets.min_shadow_days:g} days). "
        f"Eval run: {result.eval_run_id if result.eval_run_id is not None else 'none'}.",
        "",
        "| goal | metric | target | value | status | detail |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for c in result.checks:
        lines.append(f"| {c.goal} | {c.metric} | {c.target} | {c.value} | {c.status} | {c.note} |")
    failing = [c for c in result.checks if c.status != "pass"]
    if failing:
        lines += ["", "Not met: " + "; ".join(f"{c.metric} ({c.status})" for c in failing) + "."]
    return "\n".join(lines) + "\n"
