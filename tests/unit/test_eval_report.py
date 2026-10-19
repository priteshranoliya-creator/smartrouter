import pytest

from smartrouter.eval.report import format_summary, judge_agreement, summarize
from smartrouter.telemetry.models import EvalResult


def row(
    arm: str,
    score: float | None,
    *,
    passed: bool | None = None,
    rejudge: bool | None = None,
    cost: float | None = 0.01,
    latency: float | None = 100.0,
    error: str | None = None,
) -> EvalResult:
    hits: dict[str, object] | None = None
    if passed is not None:
        hits = {"judge": {"pass": passed, "score": score}}
        if rejudge is not None:
            hits["rejudge"] = {"pass": rejudge}
    return EvalResult(
        run_id=1,
        item_id="g-0001",
        arm=arm,
        model_used="m",
        answer="a",
        judge_score=score,
        rubric_hits=hits,
        cost_usd=cost,
        latency_ms=latency,
        error=error,
    )


RESULTS = [
    row("opus", 1.0, passed=True, cost=0.03, latency=200.0, rejudge=True),
    row("opus", 0.5, passed=False, cost=0.03, latency=400.0, rejudge=True),
    row("opus", 0.75, passed=True, cost=0.03, latency=300.0),
    row("haiku", 1.0, passed=True, cost=0.002, latency=50.0, rejudge=False),
    row("haiku", None, cost=None, latency=None, error="answer: boom"),
]


def test_summarize_by_hand() -> None:
    opus, haiku = summarize(RESULTS)
    assert (opus.arm, opus.n, opus.scored, opus.errors) == ("opus", 3, 3, 0)
    assert opus.mean_score == pytest.approx(0.75)
    assert opus.pass_rate == pytest.approx(2 / 3)
    assert opus.total_cost_usd == pytest.approx(0.09)
    assert opus.mean_latency_ms == pytest.approx(300.0)
    assert (opus.rejudged, opus.judge_agreement) == (2, 0.5)

    assert (haiku.arm, haiku.n, haiku.scored, haiku.errors) == ("haiku", 2, 1, 1)
    assert haiku.mean_score == 1.0
    assert haiku.pass_rate == 1.0
    assert haiku.total_cost_usd == pytest.approx(0.002)
    assert haiku.mean_latency_ms == 50.0
    assert (haiku.rejudged, haiku.judge_agreement) == (1, 0.0)


def test_judge_agreement_is_the_share_of_rejudged_rows_with_matching_pass() -> None:
    assert judge_agreement(RESULTS) == pytest.approx(1 / 3)
    assert judge_agreement([row("a", 1.0, passed=True)]) is None


def test_arm_with_only_errors_has_no_means() -> None:
    (only,) = summarize([row("x", None, cost=None, latency=None, error="e")])
    assert (only.n, only.scored, only.errors) == (1, 0, 1)
    assert (only.mean_score, only.pass_rate, only.mean_latency_ms, only.judge_agreement) == (None,) * 4
    assert only.total_cost_usd == 0.0


def test_summarize_nothing_is_empty() -> None:
    assert summarize([]) == []


def test_format_summary_lists_each_arm_and_agreement() -> None:
    text = format_summary(summarize(RESULTS))
    lines = text.splitlines()
    assert lines[0].split() == ["arm", "n", "score", "pass", "cost", "$", "latency", "ms", "errors"]
    assert lines[1].split() == ["opus", "3", "0.75", "67%", "0.0900", "300", "0"]
    assert lines[2].split() == ["haiku", "2", "1.00", "100%", "0.0020", "50", "1"]
    assert lines[3] == "judge agreement: 33% over 3 re-judged rows"
