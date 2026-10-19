from collections import defaultdict
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI

from smartrouter.app import create_app
from smartrouter.settings import ACTIVE_NOT_ALLOWED, Engine, Mode
from smartrouter.telemetry.models import EvalRun, Session
from smartrouter.telemetry.repo import Repo
from tests.integration.test_proxy import settings_with_db
from tests.integration.test_shadow import WANTS_OPUS, ScriptedEngine, serving, shadow_settings
from tests.integration.test_tracking import (
    FIXTURES,
    SESSION,
    SSE,
    UPSTREAM,
    main_headers,
    prompt,
    send,
)
from tests.seeded import EVAL_ARMS, HAIKU, OPUS, REQUEST_AGES, SONNET, seed, seed_eval_run

NOW = datetime.now(UTC)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "router.db"
    with Repo.open(path) as repo:
        seed(repo, NOW)
    return path


@pytest.fixture
async def app(db_path: Path) -> AsyncIterator[FastAPI]:
    app = create_app(settings_with_db(db_path))
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://smartrouter.test"
    ) as c:
        yield c


@pytest.fixture
def upstream() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=UPSTREAM, assert_all_called=False) as mock:
        mock.post("/v1/messages").respond(200, headers=SSE, content=(FIXTURES / "text.sse").read_bytes())
        mock.head("/").respond(404)
        yield mock


def hour_of(age: timedelta) -> str:
    return (NOW - age).replace(minute=0, second=0, microsecond=0).isoformat()


def day_of(age: timedelta) -> str:
    return (NOW - age).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def buckets(window: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {b["bucket"]: b for b in window["timeseries"]}


async def test_sessions_are_listed_by_last_activity_with_their_totals(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/sessions")
    assert resp.status_code == 200
    a, b = resp.json()
    assert list(a) == [
        "id",
        "project_path",
        "started_at",
        "last_request_at",
        "current_model",
        "state",
        "turns",
        "requests",
        "cost_usd",
        "without_router_usd",
        "with_router_usd",
        "saving_usd",
    ]
    assert (a["id"], a["project_path"], a["current_model"], a["state"]) == (
        "sess-a",
        "/work/a",
        SONNET,
        "active",
    )
    assert (a["turns"], a["requests"]) == (3, 5)
    assert a["cost_usd"] == pytest.approx(4.05)
    assert a["without_router_usd"] == pytest.approx(4.05)
    assert a["with_router_usd"] == pytest.approx(3.45)
    assert a["saving_usd"] == pytest.approx(0.60)
    assert (b["id"], b["turns"], b["requests"]) == ("sess-b", 3, 3)
    assert b["cost_usd"] == pytest.approx(6.10)
    assert b["without_router_usd"] == pytest.approx(9.10)  # r8 ran as routed: the opus baseline counts
    assert b["with_router_usd"] == pytest.approx(6.80)
    assert b["saving_usd"] == pytest.approx(2.30)


async def test_session_turns_carry_decision_and_requests(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/sessions/sess-a/turns")
    assert resp.status_code == 200
    first, second, third = resp.json()
    assert [t["seq"] for t in (first, second, third)] == [1, 2, 3]
    assert first["prompt"] == "refactor the cache layer"
    assert first["override"] is None
    assert first["decision"] == {
        "engine": "scaledown",
        "answers": {
            "complexity": {"choice": "A", "probabilities": {"A": 0.8, "B": 0.1, "C": 0.05, "D": 0.05}}
        },
        "engine_latency_ms": 1000.0,
        "desired_model": HAIKU,
        "chosen_model": HAIKU,
        "rule": "free_window",
        "mode": "shadow",
        "est_switch_tax_usd": 0.01,
        "est_saving_usd": 0.30,
        "tier_probability": 0.8,
    }
    main, background = first["requests"]
    assert (main["kind"], main["sent_model"], main["cost_usd"], main["counterfactual_cost_usd"]) == (
        "main",
        SONNET,
        1.00,
        0.40,
    )
    assert (main["without_router_usd"], main["with_router_usd"]) == (1.00, 0.40)
    assert (background["kind"], background["without_router_usd"], background["with_router_usd"]) == (
        "background",
        0.05,
        0.05,
    )
    assert main["cache_write_1h_tokens"] == 0
    assert second["decision"]["tier_probability"] == pytest.approx(0.65)
    assert [r["kind"] for r in second["requests"]] == ["main", "compaction"]
    assert third["decision"]["rule"] == "decision_timeout"
    assert third["decision"]["answers"] == {}
    assert third["decision"]["tier_probability"] is None
    (active,) = (await client.get("/api/sessions/sess-b/turns")).json()[2]["requests"]
    assert (active["requested_model"], active["sent_model"]) == (OPUS, SONNET)
    assert (active["without_router_usd"], active["with_router_usd"]) == (4.00, 1.00)


async def test_unknown_session_is_404_and_a_session_without_turns_is_empty(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    resp = await client.get("/api/sessions/nope/turns")
    assert resp.status_code == 404
    assert "nope" in resp.json()["error"]
    app.state.repo.ensure_session(Session(id="idle", project_path="", started_at=NOW.isoformat()))
    assert (await client.get("/api/sessions/idle/turns")).json() == []


async def test_stats_all_time_totals(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/stats")
    assert resp.status_code == 200
    s = resp.json()
    assert (s["window"], s["since"]) == ("all", None)
    assert (s["sessions"], s["turns"], s["requests"]) == (2, 6, 8)
    assert s["spend_usd"] == pytest.approx(10.15)
    assert s["without_router_usd"] == pytest.approx(13.15)
    assert s["with_router_usd"] == pytest.approx(10.25)
    assert s["saving_usd"] == pytest.approx(2.90)
    # 14500 read / (1580 input + 14500 read + 20 five-minute + 1000 one-hour writes)
    assert s["cache_hit_ratio"] == pytest.approx(14500 / 17100)
    assert s["switches"] == 3
    assert s["decisions_by_rule"] == {
        "free_window": 1,
        "same_tier": 1,
        "decision_timeout": 1,
        "upgrade": 1,
        "low_confidence": 1,
        "override": 1,
    }
    assert (s["timeouts"], s["low_confidence"]) == (1, 1)
    assert s["engine_latency_ms"] == {"p50": 1500.0, "p95": pytest.approx(2800.0)}
    assert s["tier_probability_histogram"] == [0, 0, 0, 0, 0, 2, 1, 0, 1, 1]


async def test_stats_by_model(client: httpx.AsyncClient) -> None:
    s = (await client.get("/api/stats")).json()
    by_model = {m["model"]: m for m in s["by_model"]}
    assert set(by_model) == {SONNET, OPUS, HAIKU}
    sonnet = by_model[SONNET]  # r1, r3, r4, r5 and r8 (asked for opus, sent sonnet)
    assert (sonnet["turns"], sonnet["requests"], sonnet["spend_usd"]) == (4, 5, pytest.approx(5.0))
    assert (sonnet["input_tokens"], sonnet["cache_read_tokens"], sonnet["cache_write_tokens"]) == (
        1150,
        10400,
        1000,
    )
    assert sonnet["cache_hit_ratio"] == pytest.approx(10400 / 12550)
    opus = by_model[OPUS]
    assert (opus["turns"], opus["requests"], opus["spend_usd"]) == (1, 1, 5.0)
    assert opus["cache_hit_ratio"] == pytest.approx(4000 / 4400)
    haiku = by_model[HAIKU]  # r2 and r6
    assert (haiku["turns"], haiku["requests"], haiku["spend_usd"]) == (2, 2, pytest.approx(0.15))
    assert (haiku["input_tokens"], haiku["cache_read_tokens"], haiku["cache_write_tokens"]) == (30, 100, 20)
    assert haiku["cache_hit_ratio"] == pytest.approx(100 / 150)


async def test_stats_switch_ledger(client: httpx.AsyncClient, app: FastAPI) -> None:
    s = (await client.get("/api/stats")).json()
    ledger = s["switch_ledger"]
    assert [(e["session_id"], e["seq"], e["from_model"], e["to_model"]) for e in ledger] == [
        ("sess-b", 1, HAIKU, OPUS),
        ("sess-a", 1, SONNET, HAIKU),
        ("sess-b", 3, OPUS, SONNET),
    ]
    assert [e["est_switch_tax_usd"] for e in ledger] == [0.20, 0.01, 0.10]
    assert [e["est_saving_usd"] for e in ledger] == [0.50, 0.30, 1.00]
    assert [e["realised_saving_usd"] for e in ledger] == [
        pytest.approx(-0.70),
        pytest.approx(0.60),
        pytest.approx(3.00),
    ]
    turn_ids = {t.seq: t.id for t in app.state.repo.list_turns("sess-b")}
    assert ledger[0]["turn_id"] == turn_ids[1]
    assert ledger[0]["created_at"] == app.state.repo.get_turn(turn_ids[1]).created_at


@pytest.mark.parametrize(
    ("window", "requests", "turns", "spend", "without", "with_router", "switches"),
    [
        ("30d", 8, 6, 10.15, 13.15, 10.25, 3),
        ("7d", 7, 5, 10.05, 13.05, 9.45, 2),
        ("24h", 7, 5, 10.05, 13.05, 9.45, 2),
        ("1h", 6, 4, 5.05, 8.05, 4.45, 2),
        ("15m", 2, 2, 1.50, 4.50, 1.50, 1),
    ],
)
async def test_stats_windows_filter_requests_and_decisions(
    client: httpx.AsyncClient,
    window: str,
    requests: int,
    turns: int,
    spend: float,
    without: float,
    with_router: float,
    switches: int,
) -> None:
    s = (await client.get("/api/stats", params={"window": window})).json()
    assert s["window"] == window
    assert s["since"] is not None
    assert (s["requests"], s["turns"], s["sessions"]) == (requests, turns, 2)
    assert s["spend_usd"] == pytest.approx(spend)
    assert s["without_router_usd"] == pytest.approx(without)
    assert s["with_router_usd"] == pytest.approx(with_router)
    assert s["saving_usd"] == pytest.approx(without - with_router)
    assert s["switches"] == switches


async def test_stats_one_hour_window_details(client: httpx.AsyncClient) -> None:
    s = (await client.get("/api/stats", params={"window": "1h"})).json()
    # 10400 read / (1160 input + 10400 read + 20 five-minute + 1000 one-hour writes)
    assert s["cache_hit_ratio"] == pytest.approx(10400 / 12580)
    assert s["decisions_by_rule"] == {"free_window": 1, "same_tier": 1, "decision_timeout": 1, "override": 1}
    assert (s["timeouts"], s["low_confidence"]) == (1, 0)
    assert s["engine_latency_ms"] == {"p50": 1000.0, "p95": pytest.approx(1450.0)}
    assert s["tier_probability_histogram"] == [0, 0, 0, 0, 0, 1, 1, 0, 1, 0]
    assert [e["seq"] for e in s["switch_ledger"]] == [1, 3]
    # hourly buckets inside a 24 h window: r1, r2, r3, r4, r5 and r8
    expected: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0])
    costs = {
        "r1": (1.00, 1.00, 0.40, 0),
        "r2": (0.05, 0.05, 0.05, 20),
        "r3": (2.00, 2.00, 2.00, 100),
        "r4": (0.50, 0.50, 0.50, 400),
        "r5": (0.50, 0.50, 0.50, 0),
        "r8": (1.00, 4.00, 1.00, 500),
    }
    for label, values in costs.items():
        bucket = expected[hour_of(REQUEST_AGES[label])]
        for i, v in enumerate(values):
            bucket[i] += v
    got = buckets(s)
    assert set(got) == set(expected)
    for key, (spend, without, with_router, writes) in expected.items():
        assert got[key]["spend_usd"] == pytest.approx(spend)
        assert got[key]["without_router_usd"] == pytest.approx(without)
        assert got[key]["with_router_usd"] == pytest.approx(with_router)
        assert got[key]["cache_write_tokens"] == writes


async def test_stats_windows_beyond_a_day_bucket_by_day(client: httpx.AsyncClient) -> None:
    s = (await client.get("/api/stats", params={"window": "all"})).json()
    old = day_of(REQUEST_AGES["r6"])
    got = buckets(s)
    assert got[old]["spend_usd"] == pytest.approx(0.10)
    assert got[old]["without_router_usd"] == pytest.approx(0.10)
    assert got[old]["with_router_usd"] == pytest.approx(0.80)
    assert all(b.endswith("T00:00:00+00:00") for b in got)
    assert sum(b["spend_usd"] for b in s["timeseries"]) == pytest.approx(10.15)
    assert sum(b["cache_write_tokens"] for b in s["timeseries"]) == 1020
    assert [b["bucket"] for b in s["timeseries"]] == sorted(got)


async def test_stats_of_an_empty_database(tmp_path: Path) -> None:
    app = create_app(settings_with_db(tmp_path / "empty.db"))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://smartrouter.test") as c,
    ):
        s = (await c.get("/api/stats", params={"window": "1h"})).json()
        sessions = (await c.get("/api/sessions")).json()
    assert sessions == []
    assert (s["sessions"], s["turns"], s["requests"], s["spend_usd"], s["cache_hit_ratio"]) == (0, 0, 0, 0, 0)
    assert s["engine_latency_ms"] == {"p50": None, "p95": None}
    assert s["tier_probability_histogram"] == [0] * 10
    assert (s["decisions_by_rule"], s["by_model"], s["timeseries"], s["switch_ledger"]) == ({}, [], [], [])


@pytest.mark.parametrize("window", ["2h", "", "week", "ALL"])
async def test_unknown_window_is_422(client: httpx.AsyncClient, window: str) -> None:
    assert (await client.get("/api/stats", params={"window": window})).status_code == 422


async def test_eval_runs_are_empty_until_a_run_exists(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/eval/runs")).json() == []
    assert (await client.get("/api/eval/runs/1")).status_code == 404


async def test_eval_run_list_has_a_summary_per_arm_newest_first(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    repo: Repo = app.state.repo
    first = seed_eval_run(repo)
    second = repo.insert_eval_run(
        EvalRun(dataset_version="v2", arms=["opus"], judge_model="j", started_at="t2")
    )
    runs = (await client.get("/api/eval/runs")).json()
    assert [r["id"] for r in runs] == [second, first]
    assert (runs[0]["n_items"], runs[0]["summary"]) == (0, [])
    older = runs[1]
    assert (older["arms"], older["n_items"], older["judge_model"]) == (EVAL_ARMS, 4, "judge-m")
    by_arm = {s["arm"]: s for s in older["summary"]}
    assert list(by_arm) == EVAL_ARMS
    assert by_arm["router"]["mean_score"] == pytest.approx(0.8)
    assert by_arm["router"]["total_cost_usd"] == pytest.approx(0.03)
    assert by_arm["router"]["mean_cost_usd"] == pytest.approx(0.0075)


async def test_eval_run_detail_matches_the_report_functions(client: httpx.AsyncClient, app: FastAPI) -> None:
    repo: Repo = app.state.repo
    run_id = seed_eval_run(repo)
    detail = (await client.get(f"/api/eval/runs/{run_id}")).json()

    assert set(detail) == {"run", "arms", "quality_cost", "routing", "agreement", "items"}
    assert detail["run"]["dataset_version"] == "v1"
    assert detail["run"]["arms"] == EVAL_ARMS
    assert [a["arm"] for a in detail["arms"]] == EVAL_ARMS
    assert detail["arms"][2]["pass_rate"] == pytest.approx(0.75)
    assert {p["arm"]: p["mean_score"] for p in detail["quality_cost"]}["oracle"] == pytest.approx(0.9)
    assert detail["routing"]["tiers"] == ["haiku", "sonnet", "opus"]
    assert detail["routing"]["n"] == 4
    assert detail["routing"]["accuracy"] == pytest.approx(0.75)
    assert detail["routing"]["confusion"]["sonnet"] == {"haiku": 1, "sonnet": 1, "opus": 0}
    assert detail["agreement"] == {"n": 2, "rate": 0.5}

    items = detail["items"]
    assert [i["item_id"] for i in items] == ["g-0001", "g-0002", "g-0003", "g-0004"]
    second = items[1]
    assert (second["expected_tier"], second["predicted_tier"]) == ("sonnet", "haiku")
    assert list(second["arms"]) == EVAL_ARMS
    router = second["arms"]["router"]
    assert router["answer"] == "router answers g-0002"
    assert (router["score"], router["passed"], router["cost_usd"], router["latency_ms"]) == (
        0.4,
        False,
        0.002,
        100.0,
    )
    assert router["model_used"] == HAIKU
    assert router["rubric"]["judge"]["points"][1] == {"index": 2, "verdict": "wrong", "reason": "r2"}
    assert second["arms"]["haiku"]["rubric"]["rejudge"]["pass"] is False


async def test_unknown_eval_run_is_404(client: httpx.AsyncClient) -> None:
    res = await client.get("/api/eval/runs/99")
    assert res.status_code == 404
    assert res.json() == {"error": "unknown eval run 99"}


async def test_mode_get_and_put(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/mode")).json() == {"mode": "off"}
    resp = await client.put("/api/mode", json={"mode": "off"})
    assert (resp.status_code, resp.json()) == (200, {"mode": "off"})
    assert (await client.put("/api/mode", json={"mode": "turbo"})).status_code == 422
    assert (await client.put("/api/mode", json={})).status_code == 422


async def test_active_mode_is_refused_and_leaves_the_mode_alone(tmp_path: Path) -> None:
    async with serving(tmp_path, ScriptedEngine(WANTS_OPUS)) as s:
        resp = await s.client.put("/api/mode", json={"mode": "active"})
        mode = (await s.client.get("/api/mode")).json()
    assert resp.status_code == 409
    assert resp.json() == {"error": ACTIVE_NOT_ALLOWED}
    assert mode == {"mode": "shadow"}


async def test_switching_to_off_applies_to_the_next_turn(tmp_path: Path, upstream: respx.MockRouter) -> None:
    engine = ScriptedEngine(WANTS_OPUS)
    async with serving(tmp_path, engine) as s:
        first = await prompt(s.client, "before", "p1")
        await send(s.client, headers=main_headers("p1"))
        resp = await s.client.put("/api/mode", json={"mode": "off"})
        health = (await s.client.get("/healthz")).json()
        second = await prompt(s.client, "after", "p2")
        await send(s.client, headers=main_headers("p2"))
        decided = (s.repo.get_decision(first), s.repo.get_decision(second))
        calls = len(engine.calls)
        decide = await s.client.post("/api/decide", json={"prompt": "hello"})
    assert resp.json() == {"mode": "off"}
    assert (health["mode"], health["engine"]) == ("off", "off")
    assert decided[0] is not None
    assert decided[1] is None
    assert calls == 1
    assert decide.status_code == 503


async def test_switching_off_to_shadow_builds_the_engine_and_decides(
    tmp_path: Path, upstream: respx.MockRouter
) -> None:
    app = create_app(shadow_settings(tmp_path / "router.db", mode=Mode.OFF))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://smartrouter.test") as c,
    ):
        assert app.state.decisions is None
        turn_before = await prompt(c, "before", "p1", SESSION)
        await send(c, headers=main_headers("p1"))
        resp = await c.put("/api/mode", json={"mode": "shadow"})
        health = (await c.get("/healthz")).json()
        turn_after = await prompt(c, "after", "p2")
        await send(c, headers=main_headers("p2"))
        repo = app.state.repo
        before, after = repo.get_decision(turn_before), repo.get_decision(turn_after)
    assert resp.json() == {"mode": "shadow"}
    assert (health["mode"], health["engine"]) == ("shadow", "ready")
    assert before is None
    assert after is not None
    assert (after.engine, after.mode) == ("fake", "shadow")


async def test_switching_on_without_a_usable_engine_is_409_and_keeps_the_mode(tmp_path: Path) -> None:
    app = create_app(shadow_settings(tmp_path / "router.db", mode=Mode.OFF, engine=Engine.SCALEDOWN))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://smartrouter.test") as c,
    ):
        resp = await c.put("/api/mode", json={"mode": "shadow"})
        mode = (await c.get("/api/mode")).json()
        assert app.state.decisions is None
    assert resp.status_code == 409
    assert "SCALEDOWN_API_KEY" in resp.json()["error"]
    assert mode == {"mode": "off"}


async def test_healthz_is_ok_when_the_upstream_answers_with_any_status(
    client: httpx.AsyncClient, upstream: respx.MockRouter
) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "mode": "off", "engine": "off", "db": "ok", "upstream": "ok"}


async def test_healthz_is_503_when_the_upstream_is_unreachable(
    client: httpx.AsyncClient, upstream: respx.MockRouter
) -> None:
    upstream.head("/").mock(side_effect=httpx.ConnectError("down"))
    resp = await client.get("/healthz")
    assert resp.status_code == 503
    assert resp.json() == {
        "status": "degraded",
        "mode": "off",
        "engine": "off",
        "db": "ok",
        "upstream": "error",
    }


async def test_healthz_caches_the_upstream_probe_for_30_seconds(
    client: httpx.AsyncClient, app: FastAPI, upstream: respx.MockRouter
) -> None:
    head = upstream.head("/")
    await client.get("/healthz")
    await client.get("/healthz")
    assert head.call_count == 1
    kind, when = "ok", app.state.upstream_probe[0]
    app.state.upstream_probe = (when - 31, kind)
    await client.get("/healthz")
    assert head.call_count == 2


async def test_healthz_is_503_when_the_database_cannot_be_written(
    client: httpx.AsyncClient, app: FastAPI, upstream: respx.MockRouter
) -> None:
    app.state.repo.close()
    resp = await client.get("/healthz")
    assert resp.status_code == 503
    body = resp.json()
    assert (body["status"], body["db"], body["upstream"]) == ("degraded", "error", "ok")
