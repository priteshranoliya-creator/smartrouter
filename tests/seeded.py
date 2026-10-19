"""A realistic seeded database for the dashboard API and export tests.

Two sessions, six turns with shadow and one active decision, main/background/compaction requests spread
over hours and days. Ages are relative to `now`, so windows can be checked against known contents.

| req | session | turn (age)   | kind       | sent    | age    | cost | counterfactual | mode   |
| r1  | A       | A1 (46 min)  | main       | sonnet  | 45 min | 1.00 | 0.40           | shadow |
| r2  | A       | A1           | background | haiku   | 44 min | 0.05 | -              | shadow |
| r3  | A       | A2 (20 min)  | main       | sonnet  | 19 min | 2.00 | 2.00           | shadow |
| r4  | A       | A2           | compaction | sonnet  | 18 min | 0.50 | -              | shadow |
| r5  | A       | A3 (6 min)   | main       | sonnet  | 5 min  | 0.50 | 0.50           | shadow |
| r6  | B       | B1 (10 days) | main       | haiku   | 10 d   | 0.10 | 0.80           | shadow |
| r7  | B       | B2 (3 h)     | main       | opus    | 3 h    | 5.00 | 5.00           | shadow |
| r8  | B       | B3 (11 min)  | main       | sonnet* | 10 min | 1.00 | 4.00           | active |
(*asked for opus)
"""

from __future__ import annotations

from datetime import datetime, timedelta

from smartrouter.telemetry.models import Decision, EvalResult, EvalRun, Request, Session, Turn
from smartrouter.telemetry.repo import Repo

OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"

MIN = timedelta(minutes=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)

# (request label, age) as seeded, for tests that bucket by hour or day.
REQUEST_AGES = {
    "r1": 45 * MIN,
    "r2": 44 * MIN,
    "r3": 19 * MIN,
    "r4": 18 * MIN,
    "r5": 5 * MIN,
    "r6": 10 * DAY,
    "r7": 3 * HOUR,
    "r8": 10 * MIN,
}


def seed(repo: Repo, now: datetime) -> None:
    def at(age: timedelta) -> str:
        return (now - age).isoformat()

    repo.upsert_session(
        Session(
            id="sess-a",
            project_path="/work/a",
            started_at=at(46 * MIN),
            current_model=SONNET,
            last_request_at=at(5 * MIN),
        )
    )
    repo.upsert_session(
        Session(
            id="sess-b",
            project_path="/work/b",
            started_at=at(10 * DAY),
            current_model=SONNET,
            last_request_at=at(10 * MIN),
        )
    )
    turns = {
        "A1": ("sess-a", 1, 46 * MIN, "refactor the cache layer"),
        "A2": ("sess-a", 2, 20 * MIN, "now add tests"),
        "A3": ("sess-a", 3, 6 * MIN, "rename the helper"),
        "B1": ("sess-b", 1, 10 * DAY, "list the files"),
        "B2": ("sess-b", 2, 3 * HOUR + MIN, "design the migration"),
        "B3": ("sess-b", 3, 11 * MIN, "@sonnet explain the diff"),
    }
    turn_id = {
        label: repo.insert_turn(
            Turn(
                session_id=session,
                seq=seq,
                prompt=prompt,
                override="sonnet" if label == "B3" else None,
                created_at=at(age),
            )
        )
        for label, (session, seq, age, prompt) in turns.items()
    }

    def decision(
        label: str,
        rule: str,
        chosen: str,
        probabilities: dict[str, float] | None,
        latency: float | None,
        tax: float | None = None,
        saving: float | None = None,
        mode: str = "shadow",
    ) -> None:
        answers = (
            {}
            if probabilities is None
            else {
                "complexity": {
                    "choice": max(probabilities, key=lambda k: probabilities[k]),
                    "probabilities": probabilities,
                }
            }
        )
        repo.insert_decision(
            Decision(
                turn_id=turn_id[label],
                engine="scaledown",
                answers=answers,
                engine_latency_ms=latency,
                desired_model=None if probabilities is None else chosen,
                chosen_model=chosen,
                rule=rule,
                mode=mode,
                est_switch_tax_usd=tax,
                est_saving_usd=saving,
            )
        )

    decision("A1", "free_window", HAIKU, {"A": 0.8, "B": 0.1, "C": 0.05, "D": 0.05}, 1000.0, 0.01, 0.30)
    decision("A2", "same_tier", SONNET, {"A": 0.1, "B": 0.3, "C": 0.35, "D": 0.25}, 1500.0)
    decision("A3", "decision_timeout", SONNET, None, None)
    decision("B1", "upgrade", OPUS, {"A": 0.02, "B": 0.03, "C": 0.03, "D": 0.92}, 3000.0, 0.20, 0.50)
    decision("B2", "low_confidence", OPUS, {"A": 0.1, "B": 0.2, "C": 0.15, "D": 0.55}, 2000.0)
    decision(
        "B3", "override", SONNET, {"A": 0.1, "B": 0.2, "C": 0.3, "D": 0.4}, 500.0, 0.10, 1.00, mode="active"
    )

    def request(
        label: str,
        session: str,
        turn: str,
        kind: str,
        requested: str,
        sent: str,
        tokens: tuple[int, int, int, int, int],
        cost: float,
        counterfactual: float | None,
        status: int = 200,
    ) -> None:
        input_t, output_t, read, w5, w1h = tokens
        repo.insert_request(
            Request(
                turn_id=turn_id[turn],
                session_id=session,
                kind=kind,
                requested_model=requested,
                sent_model=sent,
                provider="anthropic",
                input_tokens=input_t,
                output_tokens=output_t,
                cache_read_tokens=read,
                cache_write_5m_tokens=w5,
                cache_write_1h_tokens=w1h,
                cost_usd=cost,
                counterfactual_cost_usd=counterfactual,
                status=status,
                created_at=at(REQUEST_AGES[label]),
            )
        )

    request("r1", "sess-a", "A1", "main", SONNET, SONNET, (100, 50, 900, 0, 0), 1.00, 0.40)
    request("r2", "sess-a", "A1", "background", HAIKU, HAIKU, (10, 5, 0, 20, 0), 0.05, None)
    request("r3", "sess-a", "A2", "main", SONNET, SONNET, (200, 80, 1800, 0, 100), 2.00, 2.00)
    request("r4", "sess-a", "A2", "compaction", SONNET, SONNET, (50, 20, 0, 0, 400), 0.50, None)
    request("r5", "sess-a", "A3", "main", SONNET, SONNET, (300, 100, 2700, 0, 0), 0.50, 0.50)
    request("r6", "sess-b", "B1", "main", HAIKU, HAIKU, (20, 10, 100, 0, 0), 0.10, 0.80)
    request("r7", "sess-b", "B2", "main", OPUS, OPUS, (400, 200, 4000, 0, 0), 5.00, 5.00)
    request("r8", "sess-b", "B3", "main", OPUS, SONNET, (500, 250, 5000, 0, 500), 1.00, 4.00)


# Eval run, one row per (item, arm). The router and oracle arms answer with their tier's model, so a
# shared model repeats its cost and latency. Cell: (model, score, cost, latency ms).
Cell = tuple[str, float, float, float]
_COST = {"haiku": (HAIKU, 2e-3, 100.0), "sonnet": (SONNET, 6e-3, 200.0), "opus": (OPUS, 2e-2, 400.0)}


def _cell(tier: str, score: float) -> Cell:
    model, cost, latency = _COST[tier]
    return (model, score, cost, latency)


EVAL_ARMS = ["haiku", "opus", "router", "oracle"]
# (item, expected tier, router tier, {arm: cell}); the haiku arm is always haiku, the opus arm always opus
EVAL_ITEMS: list[tuple[str, str, str, dict[str, Cell]]] = [
    (
        "g-0001",
        "haiku",
        "haiku",
        {
            "haiku": _cell("haiku", 1.0),
            "opus": _cell("opus", 1.0),
            "router": _cell("haiku", 1.0),
            "oracle": _cell("haiku", 1.0),
        },
    ),
    (
        "g-0002",
        "sonnet",
        "haiku",
        {
            "haiku": _cell("haiku", 0.4),
            "opus": _cell("opus", 1.0),
            "router": _cell("haiku", 0.4),
            "oracle": _cell("sonnet", 0.8),
        },
    ),
    (
        "g-0003",
        "opus",
        "opus",
        {
            "haiku": _cell("haiku", 0.0),
            "opus": _cell("opus", 1.0),
            "router": _cell("opus", 1.0),
            "oracle": _cell("opus", 1.0),
        },
    ),
    (
        "g-0004",
        "sonnet",
        "sonnet",
        {
            "haiku": _cell("haiku", 0.8),
            "opus": _cell("opus", 1.0),
            "router": _cell("sonnet", 0.8),
            "oracle": _cell("sonnet", 0.8),
        },
    ),
]
# (item, arm) rows the second judge re-graded, and what it said.
EVAL_REJUDGED = {("g-0002", "haiku"): False, ("g-0001", "opus"): False}


def seed_eval_run(repo: Repo, started_at: str = "2026-10-19T10:00:00+00:00") -> int:
    """A finished run with the table above. By hand: haiku mean score 0.55, router 0.8, oracle 0.9, opus 1.0;
    routing accuracy 3/4; the second judge agrees on 1 of its 2 rows."""
    run_id = repo.insert_eval_run(
        EvalRun(dataset_version="v1", arms=EVAL_ARMS, judge_model="judge-m", started_at=started_at)
    )
    for item_id, expected, predicted, by_arm in EVAL_ITEMS:
        for arm in EVAL_ARMS:
            model, score, cost, latency = by_arm[arm]
            rubric: dict[str, object] = {
                "judge": {
                    "model": "judge-m",
                    "score": score,
                    "pass": score >= 0.7,
                    "points": [
                        {"index": 1, "verdict": "hit" if score >= 0.5 else "miss", "reason": "r1"},
                        {"index": 2, "verdict": "hit" if score >= 0.7 else "wrong", "reason": "r2"},
                    ],
                }
            }
            if (item_id, arm) in EVAL_REJUDGED:
                rubric["rejudge"] = {"model": "judge-2", "pass": EVAL_REJUDGED[(item_id, arm)]}
            repo.insert_eval_result(
                EvalResult(
                    run_id=run_id,
                    item_id=item_id,
                    arm=arm,
                    model_used=model,
                    answer=f"{arm} answers {item_id}",
                    judge_score=score,
                    rubric_hits=rubric,
                    cost_usd=cost,
                    latency_ms=latency,
                    expected_tier=expected,
                    predicted_tier=predicted,
                )
            )
    repo.finish_eval_run(run_id, "2026-10-19T10:30:00+00:00")
    return run_id
