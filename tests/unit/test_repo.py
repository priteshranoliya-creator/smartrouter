import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from smartrouter.telemetry.models import (
    Compaction,
    Decision,
    EvalResult,
    EvalRun,
    Request,
    Session,
    SessionModel,
    Turn,
)
from smartrouter.telemetry.repo import Repo

T0 = "2026-10-19T00:00:00+00:00"
T1 = "2026-10-19T00:00:01+00:00"
T2 = "2026-10-19T00:00:02+00:00"


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[Repo]:
    with Repo.open(tmp_path / "r.db") as r:
        r.upsert_session(Session(id="s1", project_path="/p", started_at=T0))
        yield r


def test_session_round_trip_and_upsert(repo: Repo) -> None:
    full = Session(
        id="s2",
        project_path="/q",
        started_at=T0,
        current_model="opus",
        state="compacting",
        last_request_at=T1,
        last_switch_turn=3,
    )
    repo.upsert_session(full)
    assert repo.get_session("s2") == full
    updated = replace(full, current_model="haiku", last_switch_turn=4)
    repo.upsert_session(updated)
    assert repo.get_session("s2") == updated
    assert repo.get_session("missing") is None


def test_session_state_defaults_to_active_and_can_change(repo: Repo) -> None:
    session = repo.get_session("s1")
    assert session is not None
    assert session.state == "active"
    repo.set_session_state("s1", "ended")
    ended = repo.get_session("s1")
    assert ended is not None
    assert ended.state == "ended"


def test_session_models_round_trip_and_touch_updates(repo: Repo) -> None:
    opus = SessionModel(session_id="s1", model="opus", last_used_at=T1, prefix_tokens=36000)
    haiku = SessionModel(session_id="s1", model="haiku", last_used_at=T0)
    repo.touch_session_model(opus)
    repo.touch_session_model(haiku)
    assert repo.get_session_models("s1") == [haiku, opus]
    touched = replace(opus, last_used_at=T2, prefix_tokens=40000)
    repo.touch_session_model(touched)
    assert repo.get_session_models("s1") == [haiku, touched]


def test_turn_round_trip_and_ordering(repo: Repo) -> None:
    second = Turn(session_id="s1", seq=2, prompt="b", created_at=T2)
    first = Turn(session_id="s1", seq=1, prompt_id="p-1", prompt="a", override="opus", created_at=T1)
    second_id = repo.insert_turn(second)
    first_id = repo.insert_turn(first)
    assert repo.get_turn(first_id) == replace(first, id=first_id)
    assert repo.list_turns("s1") == [replace(first, id=first_id), replace(second, id=second_id)]
    assert repo.get_turn(9999) is None


def test_decision_round_trip_with_json_answers(repo: Repo) -> None:
    turn_id = repo.insert_turn(Turn(session_id="s1", seq=1, prompt="a", created_at=T1))
    decision = Decision(
        turn_id=turn_id,
        engine="scaledown",
        answers={"complexity": {"choice": "hard", "probabilities": {"hard": 0.7, "easy": 0.3}}},
        engine_latency_ms=1200.5,
        desired_model="opus",
        chosen_model="sonnet",
        rule="payback",
        mode="shadow",
        est_switch_tax_usd=0.12,
        est_saving_usd=0.05,
    )
    repo.insert_decision(decision)
    assert repo.get_decision(turn_id) == decision
    assert repo.get_decision(9999) is None


def test_request_round_trip_and_ordering(repo: Repo) -> None:
    turn_id = repo.insert_turn(Turn(session_id="s1", seq=1, prompt="a", created_at=T1))
    full = Request(
        turn_id=turn_id,
        session_id="s1",
        kind="main",
        requested_model="opus",
        sent_model="sonnet",
        provider="anthropic",
        input_tokens=10,
        output_tokens=20,
        cache_read_tokens=30,
        cache_write_5m_tokens=40,
        cache_write_1h_tokens=50,
        cost_usd=0.5,
        counterfactual_cost_usd=0.9,
        latency_ms=800.0,
        ttft_ms=300.0,
        status=200,
        error=None,
        created_at=T2,
    )
    minimal = Request(
        session_id="s1",
        kind="background",
        requested_model="haiku",
        sent_model="haiku",
        provider="anthropic",
        cost_usd=0.01,
        created_at=T1,
    )
    full_id = repo.insert_request(full)
    minimal_id = repo.insert_request(minimal)
    assert repo.list_requests("s1") == [replace(minimal, id=minimal_id), replace(full, id=full_id)]
    assert repo.list_requests("other") == []


def test_compaction_round_trip(repo: Repo) -> None:
    compaction = Compaction(session_id="s1", trigger="auto", prefix_tokens_before=150000, created_at=T1)
    compaction_id = repo.insert_compaction(compaction)
    assert repo.list_compactions("s1") == [replace(compaction, id=compaction_id)]
    assert repo.list_compactions("other") == []


def test_eval_run_round_trip_and_finish(repo: Repo) -> None:
    run = EvalRun(
        dataset_version="v1", arms=["router", "always-opus"], judge_model="gemini-pro", started_at=T0
    )
    run_id = repo.insert_eval_run(run)
    assert repo.get_eval_run(run_id) == replace(run, id=run_id)
    repo.finish_eval_run(run_id, T2)
    assert repo.get_eval_run(run_id) == replace(run, id=run_id, finished_at=T2)
    assert repo.get_eval_run(9999) is None


def test_eval_result_round_trip(repo: Repo) -> None:
    run_id = repo.insert_eval_run(EvalRun(dataset_version="v1", arms=["a"], judge_model="j", started_at=T0))
    full = EvalResult(
        run_id=run_id,
        item_id="item-1",
        arm="a",
        model_used="sonnet",
        answer="42",
        judge_score=0.75,
        rubric_hits={"correct": True, "points": [1, 2]},
        cost_usd=0.02,
        latency_ms=900.0,
        expected_tier="medium",
        predicted_tier="hard",
        error="judge: bad json",
    )
    bare = EvalResult(run_id=run_id, item_id="item-2", arm="a", model_used="haiku", answer="?")
    full_id = repo.insert_eval_result(full)
    bare_id = repo.insert_eval_result(bare)
    assert repo.list_eval_results(run_id) == [replace(full, id=full_id), replace(bare, id=bare_id)]


def test_delete_eval_errors_removes_only_failed_rows_of_that_run(repo: Repo) -> None:
    first = repo.insert_eval_run(EvalRun(dataset_version="v", arms=["a"], judge_model="j", started_at=T0))
    second = repo.insert_eval_run(EvalRun(dataset_version="v", arms=["a"], judge_model="j", started_at=T0))
    good = EvalResult(run_id=first, item_id="i1", arm="a", model_used="m", answer="x")
    bad = EvalResult(run_id=first, item_id="i2", arm="a", model_used="m", answer="", error="boom")
    other = EvalResult(run_id=second, item_id="i2", arm="a", model_used="m", answer="", error="boom")
    for row in (good, bad, other):
        repo.insert_eval_result(row)
    assert repo.delete_eval_errors(first) == 1
    assert [r.item_id for r in repo.list_eval_results(first)] == ["i1"]
    assert len(repo.list_eval_results(second)) == 1


def test_judge_score_outside_unit_interval_is_rejected(repo: Repo) -> None:
    run_id = repo.insert_eval_run(EvalRun(dataset_version="v1", arms=["a"], judge_model="j", started_at=T0))
    bad = EvalResult(run_id=run_id, item_id="i", arm="a", model_used="m", answer="x", judge_score=1.5)
    with pytest.raises(sqlite3.IntegrityError):
        repo.insert_eval_result(bad)


def test_data_persists_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "r.db"
    session = Session(id="s9", project_path="/p", started_at=T0)
    with Repo.open(path) as first:
        first.upsert_session(session)
    with Repo.open(path) as second:
        assert second.get_session("s9") == session


def test_ensure_session_keeps_the_row_but_fills_an_empty_project_path(repo: Repo) -> None:
    repo.ensure_session(Session(id="s9", project_path="", started_at=T0))
    repo.ensure_session(Session(id="s9", project_path="/proj", started_at=T2))
    repo.ensure_session(Session(id="s9", project_path="/other", started_at=T2))
    assert repo.get_session("s9") == Session(id="s9", project_path="/proj", started_at=T0)


def test_turn_lookups(repo: Repo) -> None:
    assert repo.next_turn_seq("s1") == 1
    assert repo.latest_turn("s1") is None
    ids = [
        repo.insert_turn(Turn(session_id="s1", seq=seq, prompt_id=f"p{seq}", prompt="x", created_at=T0))
        for seq in (1, 2)
    ]
    assert repo.next_turn_seq("s1") == 3
    assert repo.next_turn_seq("other") == 1
    latest = repo.latest_turn("s1")
    assert latest is not None
    assert latest.id == ids[1]
    first = repo.get_turn_by_prompt_id("s1", "p1")
    assert first is not None
    assert first.id == ids[0]
    assert repo.get_turn_by_prompt_id("s1", "nope") is None
    assert repo.get_turn_by_prompt_id("other", "p1") is None


def test_mark_session_request_sets_model_and_ends_compaction(repo: Repo) -> None:
    repo.set_session_state("s1", "compacting")
    repo.mark_session_request("s1", "opus", T1)
    session = repo.get_session("s1")
    assert session is not None
    assert (session.current_model, session.last_request_at, session.state) == ("opus", T1, "active")
    repo.set_session_state("s1", "ended")
    repo.mark_session_request("s1", "haiku", T2)
    ended = repo.get_session("s1")
    assert ended is not None
    assert (ended.current_model, ended.state) == ("haiku", "ended")


def test_get_session_model(repo: Repo) -> None:
    warm = SessionModel(session_id="s1", model="opus", last_used_at=T1, prefix_tokens=500)
    repo.touch_session_model(warm)
    assert repo.get_session_model("s1", "opus") == warm
    assert repo.get_session_model("s1", "haiku") is None


def test_insert_decision_if_absent_keeps_the_first_row(repo: Repo) -> None:
    turn_id = repo.insert_turn(Turn(session_id="s1", seq=1, prompt="a", created_at=T1))
    first = Decision(
        turn_id=turn_id, engine="fake", answers={}, chosen_model="opus", rule="payback", mode="shadow"
    )
    assert repo.insert_decision_if_absent(first) is True
    assert repo.insert_decision_if_absent(replace(first, chosen_model="haiku")) is False
    assert repo.get_decision(turn_id) == first


def test_session_statistics_for_the_policy(repo: Repo) -> None:
    repo.upsert_session(Session(id="s2", project_path="/p", started_at=T0))
    assert repo.turns_per_session() == (None, 0)
    assert repo.mean_main_output_tokens("s1") is None
    ids = [
        repo.insert_turn(Turn(session_id=sid, seq=seq, prompt=f"{sid}-{seq}", created_at=T1))
        for sid, count in (("s1", 3), ("s2", 1))
        for seq in range(1, count + 1)
    ]
    assert repo.turns_per_session() == (2.0, 2)
    assert repo.previous_turn_prompt("s1", 3) == "s1-2"
    assert repo.previous_turn_prompt("s1", 1) is None

    def request(kind: str, output: int | None, turn_id: int) -> Request:
        return Request(
            turn_id=turn_id,
            session_id="s1",
            kind=kind,  # type: ignore[arg-type]
            requested_model="m",
            sent_model="m",
            provider="anthropic",
            output_tokens=output,
            cost_usd=0.0,
            created_at=T1,
        )

    for row in (request("main", 10, ids[0]), request("main", 30, ids[0]), request("main", None, ids[0])):
        repo.insert_request(row)
    repo.insert_request(request("background", 1000, ids[0]))
    assert repo.mean_main_output_tokens("s1") == 20.0
    assert repo.main_request_count(ids[0]) == 3
    assert repo.main_request_count(ids[1]) == 0
