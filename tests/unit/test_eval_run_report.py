from pathlib import Path

import pytest

from smartrouter.eval.report import (
    agreement,
    item_rows,
    quality_cost_points,
    render_run_report,
    routing,
    run_report,
    summarize,
)
from smartrouter.telemetry.models import EvalResult, EvalRun
from smartrouter.telemetry.repo import Repo
from tests.seeded import EVAL_ARMS, seed_eval_run

TIERS = ["haiku", "sonnet", "opus"]


@pytest.fixture
def stored(tmp_path: Path) -> tuple[Repo, int]:
    repo = Repo.open(tmp_path / "r.db")
    return repo, seed_eval_run(repo)


def test_arm_means_by_hand(stored: tuple[Repo, int]) -> None:
    repo, run_id = stored
    by_arm = {s.arm: s for s in summarize(repo.list_eval_results(run_id))}
    assert by_arm["haiku"].mean_score == pytest.approx(0.55)
    assert by_arm["router"].mean_score == pytest.approx(0.8)
    assert by_arm["oracle"].mean_score == pytest.approx(0.9)
    assert by_arm["opus"].mean_score == pytest.approx(1.0)
    assert by_arm["haiku"].pass_rate == pytest.approx(0.5)
    assert by_arm["router"].pass_rate == pytest.approx(0.75)
    assert by_arm["router"].total_cost_usd == pytest.approx(0.030)
    assert by_arm["router"].mean_cost_usd == pytest.approx(0.0075)
    assert by_arm["oracle"].total_cost_usd == pytest.approx(0.034)
    assert by_arm["oracle"].mean_cost_usd == pytest.approx(0.0085)
    assert by_arm["oracle"].mean_latency_ms == pytest.approx(225.0)


def test_mean_cost_ignores_rows_without_a_cost() -> None:
    rows = [
        EvalResult(run_id=1, item_id="a", arm="x", model_used="m", answer="", cost_usd=0.02),
        EvalResult(run_id=1, item_id="b", arm="x", model_used="m", answer="", error="boom"),
    ]
    (x,) = summarize(rows)
    assert (x.n, x.total_cost_usd, x.mean_cost_usd) == (2, 0.02, 0.02)


def test_confusion_counts_each_item_once_expected_by_predicted(stored: tuple[Repo, int]) -> None:
    repo, run_id = stored
    r = routing(repo.list_eval_results(run_id), TIERS)
    assert r.n == 4
    assert r.accuracy == pytest.approx(0.75)
    assert r.confusion == {
        "haiku": {"haiku": 1, "sonnet": 0, "opus": 0},
        "sonnet": {"haiku": 1, "sonnet": 1, "opus": 0},
        "opus": {"haiku": 0, "sonnet": 0, "opus": 1},
    }


def test_routing_without_predictions_has_no_accuracy() -> None:
    rows = [EvalResult(run_id=1, item_id="a", arm="x", model_used="m", answer="", expected_tier="opus")]
    r = routing(rows, TIERS)
    assert (r.n, r.accuracy) == (0, None)
    assert all(count == 0 for row in r.confusion.values() for count in row.values())


def test_agreement_is_one_of_two_rejudged_rows(stored: tuple[Repo, int]) -> None:
    repo, run_id = stored
    a = agreement(repo.list_eval_results(run_id))
    assert (a.n, a.rate) == (2, 0.5)


def test_quality_cost_has_one_point_per_scored_arm(stored: tuple[Repo, int]) -> None:
    repo, run_id = stored
    points = quality_cost_points(summarize(repo.list_eval_results(run_id)))
    assert {p.arm: (round(p.total_cost_usd, 4), round(p.mean_score, 2)) for p in points} == {
        "haiku": (0.008, 0.55),
        "opus": (0.08, 1.0),
        "router": (0.03, 0.8),
        "oracle": (0.034, 0.9),
    }


def test_items_carry_every_arms_answer_in_run_arm_order(stored: tuple[Repo, int]) -> None:
    repo, run_id = stored
    items = item_rows(repo.list_eval_results(run_id), EVAL_ARMS)
    assert [i.item_id for i in items] == ["g-0001", "g-0002", "g-0003", "g-0004"]
    second = items[1]
    assert (second.expected_tier, second.predicted_tier) == ("sonnet", "haiku")
    assert list(second.arms) == EVAL_ARMS
    haiku = second.arms["haiku"]
    assert (haiku.answer, haiku.score, haiku.passed) == ("haiku answers g-0002", 0.4, False)
    assert haiku.rubric is not None
    assert [p["verdict"] for p in haiku.rubric["judge"]["points"]] == ["miss", "wrong"]
    assert haiku.rubric["rejudge"]["pass"] is False
    assert second.arms["oracle"].model_used == "claude-sonnet-5-5"


def test_report_orders_arms_as_the_run_does(stored: tuple[Repo, int]) -> None:
    repo, run_id = stored
    run = repo.get_eval_run(run_id)
    assert run is not None
    report = run_report(run, repo.list_eval_results(run_id), TIERS)
    assert [s.arm for s in report.arms] == EVAL_ARMS
    assert [p.arm for p in report.quality_cost] == EVAL_ARMS


def test_markdown_report_lists_every_section_with_the_hand_numbers(stored: tuple[Repo, int]) -> None:
    repo, run_id = stored
    run = repo.get_eval_run(run_id)
    assert run is not None
    text = render_run_report(run_report(run, repo.list_eval_results(run_id), TIERS))
    lines = text.splitlines()
    assert lines[0] == f"# Eval run {run_id}"
    assert "| router | 4 | 0.80 | 75% | 0.0300 | 0.0075 | 200 | 0 |" in lines
    assert "| haiku | 4 | 0.55 | 50% | 0.0080 | 0.0020 | 100 | 0 |" in lines
    assert "| oracle | 0.0340 | 0.90 |" in lines
    assert "Routing accuracy 75% over 4 items (rows: expected tier, columns: router)." in lines
    assert "| expected \\ predicted | haiku | sonnet | opus |" in lines
    assert "| sonnet | 1 | 1 | 0 |" in lines
    assert "50% pass/fail agreement over 2 re-judged rows." in lines
    assert "| item | expected | router | haiku | opus | router | oracle |" in lines
    assert "| g-0002 | sonnet | haiku | 0.40 | 1.00 | 0.40 | 0.80 |" in lines


def test_markdown_report_of_a_run_with_nothing_stored(tmp_path: Path) -> None:
    with Repo.open(tmp_path / "r.db") as repo:
        run_id = repo.insert_eval_run(
            EvalRun(dataset_version="v1", arms=["a"], judge_model="j", started_at="t")
        )
        run = repo.get_eval_run(run_id)
    assert run is not None
    text = render_run_report(run_report(run, [], TIERS))
    assert "No router predictions in this run." in text
    assert "No rows were re-judged." in text
    assert "not finished" in text
