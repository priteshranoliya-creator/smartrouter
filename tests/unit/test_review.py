from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from smartrouter import cli
from smartrouter.eval.report import ArmSummary, Routing
from smartrouter.review import (
    Coverage,
    added_latencies,
    cost_checks,
    coverage,
    render,
    review,
    routing_check,
    switch_paybacks,
    visibility_checks,
)
from smartrouter.settings import Mode, ReviewConfig, Settings, load_settings
from smartrouter.telemetry.models import Decision, DecisionFact, Request, RequestRow, Turn
from smartrouter.telemetry.repo import Repo
from tests.seeded import seed

OPUS, SONNET, HAIKU = "m-opus", "m-sonnet", "m-haiku"
T0 = datetime(2026, 10, 1, tzinfo=UTC)
TARGETS = ReviewConfig(
    baseline_arm="opus",
    min_cost_reduction=0.3,
    min_quality_ratio=0.95,
    min_payback_share=0.8,
    max_added_latency_p50_ms=300,
    min_logged_share=1.0,
    min_shadow_days=7,
    min_shadow_turns=3,
)


def at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat()


def fact(
    turn_id: int,
    seq: int,
    chosen: str,
    *,
    session: str = "s",
    asked: str | None = OPUS,
    latency: float | None = 500.0,
    created: float = 0.0,
    mode: Mode = Mode.SHADOW,
) -> DecisionFact:
    return DecisionFact(
        decision=Decision(
            turn_id=turn_id,
            engine="fake",
            answers={},
            engine_latency_ms=latency,
            chosen_model=chosen,
            rule="payback",
            mode=mode,
        ),
        turn=Turn(id=turn_id, session_id=session, seq=seq, prompt="p", created_at=at(created)),
        first_main_requested_model=asked,
    )


def row(
    turn_id: int | None,
    cost: float,
    counterfactual: float | None,
    *,
    kind: str = "main",
    created: float = 0.0,
    usage: bool = True,
    status: int = 200,
    error: str | None = None,
) -> RequestRow:
    return RequestRow(
        request=Request(
            turn_id=turn_id,
            kind=kind,  # type: ignore[arg-type]
            requested_model=OPUS,
            sent_model=OPUS,
            provider="anthropic",
            input_tokens=10 if usage else None,
            output_tokens=5 if usage else None,
            cost_usd=cost,
            counterfactual_cost_usd=counterfactual,
            status=status,
            error=error,
            created_at=at(created),
        ),
        mode="shadow",
    )


def summary(arm: str, cost: float, score: float | None) -> ArmSummary:
    return ArmSummary(arm, 10, 10, 0, score, 1.0, cost, cost / 10, 100.0, 0, None)


# --- cost and quality vs the baseline ---------------------------------------------------------


@pytest.mark.parametrize(
    ("router_cost", "router_score", "statuses", "values"),
    [
        (0.60, 0.95, ("pass", "pass"), ("40%", "95%")),  # both exactly at their boundary
        (0.71, 0.95, ("fail", "pass"), ("29%", "95%")),
        (0.50, 0.90, ("pass", "fail"), ("50%", "90%")),
        (1.20, 1.10, ("fail", "pass"), ("-20%", "110%")),
    ],
)
def test_cost_and_quality_against_the_baseline(
    router_cost: float, router_score: float, statuses: tuple[str, str], values: tuple[str, str]
) -> None:
    targets = TARGETS.model_copy(update={"min_cost_reduction": 0.4})
    checks = cost_checks([summary("opus", 1.0, 1.0), summary("router", router_cost, router_score)], targets)
    assert tuple(c.status for c in checks) == statuses
    assert tuple(c.value for c in checks) == values


@pytest.mark.parametrize(
    "summaries",
    [
        [],
        [summary("opus", 1.0, 1.0)],
        [summary("router", 0.5, 1.0)],
        [summary("opus", 0.0, None), summary("router", 0.5, 1.0)],
    ],
)
def test_cost_checks_without_both_arms_have_no_data(summaries: list[ArmSummary]) -> None:
    assert [c.status for c in cost_checks(summaries, TARGETS)] == ["no data", "no data"]


# --- switch payback ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("facts", "rows", "expected"),
    [
        ([], [], []),
        # router agrees with the asked model throughout: no switch
        ([fact(1, 1, OPUS), fact(2, 2, OPUS)], [row(1, 1.0, 1.0), row(2, 1.0, 1.0)], []),
        # switch on turn 1 pays its tax back by turn 2
        ([fact(1, 1, HAIKU), fact(2, 2, HAIKU)], [row(1, 1.0, 1.5), row(2, 1.0, 0.2)], [True]),
        # switch whose cold-cache first turn is never recovered
        ([fact(1, 1, HAIKU), fact(2, 2, HAIKU)], [row(1, 1.0, 1.5), row(2, 1.0, 0.9)], [False]),
        # break-even counts as paid back
        ([fact(1, 1, HAIKU)], [row(1, 1.0, 1.0)], [True]),
        # two switches: each segment is judged on its own turns only
        (
            [fact(1, 1, HAIKU), fact(2, 2, HAIKU), fact(3, 3, OPUS)],
            [row(1, 1.0, 0.5), row(2, 1.0, 0.5), row(3, 1.0, 1.4)],
            [True, False],
        ),
        # sessions are independent and ordered by seq, not input order
        (
            [fact(2, 2, OPUS, session="a"), fact(1, 1, HAIKU, session="a"), fact(3, 1, OPUS, session="b")],
            [row(1, 1.0, 0.2), row(2, 1.0, 1.1)],
            [True, False],
        ),
        # the first turn asked for no main model: the first decision is not a switch
        ([fact(1, 1, HAIKU, asked=None), fact(2, 2, OPUS)], [row(2, 1.0, 0.5)], [True]),
        # a switch with no logged counterfactual is left out, not counted as paid
        ([fact(1, 1, SONNET)], [], []),
        ([fact(1, 1, SONNET)], [row(1, 1.0, None)], []),
        # unmeasured turns inside a measured segment do not count as break-even
        ([fact(1, 1, HAIKU), fact(2, 2, HAIKU)], [row(1, 1.0, 1.2), row(2, 1.0, None)], [False]),
    ],
)
def test_switch_paybacks(facts: list[DecisionFact], rows: list[RequestRow], expected: list[bool]) -> None:
    assert switch_paybacks(facts, rows) == expected


# --- added latency ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("engine_ms", "request_at", "expected"),
    [
        (500.0, 0.2, [300.0]),  # 200 ms head start between hook and request
        (100.0, 0.2, [0.0]),  # decision ready before the request arrives
        (None, 0.2, []),
    ],
)
def test_added_latency_is_what_is_left_of_the_decision(
    engine_ms: float | None, request_at: float, expected: list[float]
) -> None:
    waits = added_latencies([fact(1, 1, OPUS, latency=engine_ms)], [row(1, 1.0, 1.0, created=request_at)])
    assert waits == pytest.approx(expected)


def test_added_latency_uses_the_turns_first_main_request() -> None:
    rows = [
        row(1, 0.1, None, kind="background", created=0.05),
        row(1, 1, 1, created=0.4),
        row(1, 1, 1, created=0.9),
    ]
    assert added_latencies([fact(1, 1, OPUS, latency=500.0)], rows) == pytest.approx([100.0])


# --- visibility -------------------------------------------------------------------------------


def test_visibility_counts_logged_requests_and_decided_turns() -> None:
    rows = [
        row(1, 1.0, 1.0),
        row(2, 0.0, None, usage=False, status=529, error="overloaded"),  # error with message: logged
        row(3, 0.0, None, usage=False),  # 200 without usage: not logged
        row(None, 0.1, None, kind="background"),
    ]
    requests, decisions = visibility_checks([fact(1, 1, OPUS), fact(2, 2, OPUS)], rows, TARGETS)
    assert (requests.value, requests.status, requests.note) == ("75%", "fail", "3 of 4")
    assert (decisions.value, decisions.status, decisions.note) == ("67%", "fail", "2 of 3")


def test_visibility_without_traffic_has_no_data() -> None:
    assert [c.status for c in visibility_checks([], [], TARGETS)] == ["no data", "no data"]


# --- routing ----------------------------------------------------------------------------------


def routing(confusion: dict[str, dict[str, int]], accuracy: float | None) -> Routing:
    n = sum(sum(r.values()) for r in confusion.values())
    return Routing(tiers=tuple(confusion), n=n, accuracy=accuracy, confusion=confusion)


@pytest.mark.parametrize(
    ("accuracy", "status"),
    [(0.6, "fail"), (0.61, "pass"), (None, "no data")],
)
def test_routing_must_beat_the_majority_class(accuracy: float | None, status: str) -> None:
    confusion = {"haiku": {"haiku": 2, "opus": 0}, "opus": {"haiku": 3, "opus": 0}}  # opus is 60%
    check = routing_check(routing(confusion, accuracy))
    assert check.status == status
    assert "(60%)" in check.target


# --- coverage and verdict ---------------------------------------------------------------------


def test_coverage_counts_shadow_decisions_only() -> None:
    facts = [
        fact(1, 1, OPUS, created=0),
        fact(2, 1, OPUS, session="b", created=86400 * 2),
        fact(3, 2, OPUS, created=86400 * 3, mode=Mode.ACTIVE),
    ]
    assert coverage(facts) == Coverage(sessions=2, turns=2, days=pytest.approx(2.0))
    assert coverage([]) == Coverage(0, 0, 0.0)


def passing_inputs(days: float) -> dict[str, object]:
    facts = [fact(i, i, OPUS, latency=400.0, created=(i - 1) * days * 86400 / 2) for i in (1, 2, 3)]
    rows = [row(i, 1.0, 1.0, created=(i - 1) * days * 86400 / 2 + 0.2) for i in (1, 2, 3)]
    facts[0] = fact(1, 1, HAIKU, latency=400.0)  # one switch that pays back
    rows[0] = row(1, 1.0, 0.5, created=0.2)
    confusion = {"haiku": {"haiku": 2, "opus": 0}, "opus": {"haiku": 0, "opus": 1}}
    return {
        "facts": facts,
        "rows": rows,
        "summaries": [summary("opus", 1.0, 1.0), summary("router", 0.5, 1.0)],
        "routing": routing(confusion, 1.0),
        "eval_run_id": 7,
        "targets": TARGETS,
    }


def test_all_targets_met_with_enough_data_is_go() -> None:
    result = review(**passing_inputs(days=8))  # type: ignore[arg-type]
    assert [c.status for c in result.checks] == ["pass"] * 7
    assert result.go
    assert "**Verdict: GO**" in render(result, TARGETS)


def test_all_targets_met_but_too_little_data_is_no_go() -> None:
    result = review(**passing_inputs(days=1))  # type: ignore[arg-type]
    assert result.go
    assert "NO-GO (not enough shadow data yet)" in render(result, TARGETS)


def test_a_failing_target_is_named() -> None:
    inputs = passing_inputs(days=8)
    inputs["targets"] = TARGETS.model_copy(update={"max_added_latency_p50_ms": 50})
    result = review(**inputs)  # type: ignore[arg-type]
    text = render(result, inputs["targets"])  # type: ignore[arg-type]
    assert not result.go
    assert "**Verdict: NO-GO**" in text
    assert "Not met: added latency per prompt, p50 (fail)." in text


# --- CLI --------------------------------------------------------------------------------------

runner = CliRunner()


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    base = load_settings(env={})
    paths = base.router.paths.model_copy(
        update={"db": tmp_path / "router.db", "reports": tmp_path / "reports"}
    )
    router = base.router.model_copy(update={"paths": paths})
    settings = Settings(router=router, questions=base.questions, secrets=base.secrets)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    yield settings


def test_cli_writes_the_review_and_exits_1_on_no_go(settings: Settings) -> None:
    with Repo.open(settings.path("db")) as repo:
        seed(repo, datetime.now(UTC))
    result = runner.invoke(cli.app, ["review"])
    assert result.exit_code == 1, result.output
    assert "# Go/no-go review (T6.1)" in result.stdout
    assert (settings.path("reports") / "review.md").read_text() == result.stdout.rstrip("\n") + "\n"


def test_cli_unknown_run_exits_2(settings: Settings) -> None:
    with Repo.open(settings.path("db")):
        pass
    assert runner.invoke(cli.app, ["review", "--run", "99"]).exit_code == 2


def test_cli_without_database_exits_1(settings: Settings) -> None:
    assert runner.invoke(cli.app, ["review"]).exit_code == 1
