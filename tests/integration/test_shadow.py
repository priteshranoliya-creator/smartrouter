import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI

from smartrouter.app import create_app
from smartrouter.decision.engine import DecisionState, EngineError, FakeEngine
from smartrouter.providers.base import Usage
from smartrouter.settings import ROOT, ConfigError, Engine, Mode, Settings, load_settings
from smartrouter.telemetry.models import SessionModel
from smartrouter.telemetry.pricing import PriceBook, cost, counterfactual, load_pricing
from tests.integration.test_proxy import settings_with_db
from tests.integration.test_tracking import (
    FIXTURES,
    HAIKU,
    SESSION,
    SSE,
    UPSTREAM,
    body,
    drain,
    main_headers,
    prompt,
    send,
)

OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5-5"
USAGE = Usage(input_tokens=2, output_tokens=4, cache_read_tokens=35617, inference_geo="global")
WANTS_OPUS = {"A": 0.02, "B": 0.04, "C": 0.04, "D": 0.9}
WANTS_HAIKU = {"A": 0.9, "B": 0.04, "C": 0.04, "D": 0.02}


class ScriptedEngine(FakeEngine):
    """Answers `table` for the complexity question, optionally blocking on `gate` or raising `error`."""

    def __init__(
        self,
        table: dict[str, float],
        gate: asyncio.Event | None = None,
        error: Exception | None = None,
    ) -> None:
        super().__init__(load_settings(env={}).questions, lambda state: {"complexity": table})
        self.gate = gate
        self.error = error
        self.entered = asyncio.Event()

    async def decide(self, state: DecisionState) -> Any:
        self.entered.set()
        if self.error is not None:
            raise self.error
        if self.gate is not None:
            await self.gate.wait()
        return await super().decide(state)


def shadow_settings(
    db: Path, mode: Mode = Mode.SHADOW, deadline_ms: int = 2500, engine: Engine = Engine.FAKE
) -> Settings:
    base = settings_with_db(db)
    decision = base.router.decision.model_copy(update={"deadline_ms": deadline_ms, "engine": engine})
    return replace(base, router=base.router.model_copy(update={"mode": mode, "decision": decision}))


@dataclass
class Served:
    app: FastAPI
    client: httpx.AsyncClient

    @property
    def repo(self) -> Any:
        return self.app.state.repo


@asynccontextmanager
async def serving(tmp_path: Path, engine: ScriptedEngine, **options: Any) -> AsyncIterator[Served]:
    app = create_app(shadow_settings(tmp_path / "router.db", **options), engine)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://smartrouter.test"
        ) as client,
    ):
        yield Served(app, client)


@pytest.fixture
def upstream() -> Iterator[respx.Route]:
    with respx.mock(base_url=UPSTREAM, assert_all_called=False) as mock:
        yield mock.post("/v1/messages").respond(
            200, headers=SSE, content=(FIXTURES / "text.sse").read_bytes()
        )


@pytest.fixture(scope="module")
def book() -> PriceBook:
    return load_pricing(ROOT / "config/pricing.yaml")


def on_date(created_at: str) -> Any:
    return datetime.fromisoformat(created_at).date()


async def test_shadow_main_request_gets_a_decision_row_and_a_counterfactual(
    tmp_path: Path, upstream: respx.Route, book: PriceBook
) -> None:
    async with serving(tmp_path, ScriptedEngine(WANTS_OPUS)) as s:
        turn_id = await prompt(s.client, "design the cache layer", "p1")
        await send(s.client, headers=main_headers("p1"))
        decision = s.repo.get_decision(turn_id)
        (row,) = s.repo.list_requests(SESSION)
    assert decision is not None
    assert (decision.engine, decision.rule, decision.mode) == ("fake", "free_window", "shadow")
    assert (decision.desired_model, decision.chosen_model) == (OPUS, OPUS)
    assert decision.answers["complexity"]["choice"] == "D"
    assert decision.answers["complexity"]["probabilities"] == WANTS_OPUS
    assert decision.engine_latency_ms == 0.0
    assert row.requested_model == row.sent_model == HAIKU
    assert row.counterfactual_cost_usd is not None
    assert upstream.calls.call_count == 1
    assert upstream.calls.last.request.content == body()


async def test_second_main_request_reuses_the_decision_and_background_has_none(
    tmp_path: Path, upstream: respx.Route, book: PriceBook
) -> None:
    async with serving(tmp_path, ScriptedEngine(WANTS_OPUS)) as s:
        turn_id = await prompt(s.client, "hello", "p1")
        await send(s.client, headers=main_headers("p1"))
        await send(s.client, headers=main_headers("p1"))
        await send(
            s.client,
            headers={"x-claude-code-session-id": SESSION, "x-claude-code-request-class": "auxiliary"},
        )
        decisions = s.app.state.repo._all("SELECT turn_id FROM decisions", ())
        first, second, background = s.repo.list_requests(SESSION)
    assert [d[0] for d in decisions] == [turn_id]
    assert first.counterfactual_cost_usd is not None
    assert second.counterfactual_cost_usd is not None
    assert (background.kind, background.counterfactual_cost_usd) == ("background", None)
    when = on_date(second.created_at)
    assert second.counterfactual_cost_usd == pytest.approx(counterfactual(book, OPUS, USAGE, 35617, when))


async def test_proxy_forwards_before_the_engine_finishes(tmp_path: Path, upstream: respx.Route) -> None:
    gate = asyncio.Event()
    engine = ScriptedEngine(WANTS_OPUS, gate=gate)
    async with serving(tmp_path, engine) as s:
        turn_id = await prompt(s.client, "hello", "p1")
        sending = asyncio.create_task(send(s.client, headers=main_headers("p1")))
        for _ in range(200):
            if upstream.calls.call_count:
                break
            await asyncio.sleep(0.01)
        assert upstream.calls.call_count == 1
        assert engine.entered.is_set()
        assert not gate.is_set()
        assert s.repo.get_decision(turn_id) is None
        gate.set()
        await asyncio.wait_for(sending, 2)
        decision = s.repo.get_decision(turn_id)
    assert decision is not None
    assert decision.rule == "free_window"


async def test_engine_slower_than_the_deadline_is_a_timeout(tmp_path: Path, upstream: respx.Route) -> None:
    engine = ScriptedEngine(WANTS_OPUS, gate=asyncio.Event())
    async with serving(tmp_path, engine, deadline_ms=50) as s:
        turn_id = await prompt(s.client, "hello", "p1")
        await send(s.client, headers=main_headers("p1"))
        decision = s.repo.get_decision(turn_id)
        (row,) = s.repo.list_requests(SESSION)
    assert decision is not None
    assert (decision.rule, decision.chosen_model, decision.desired_model) == ("decision_timeout", HAIKU, None)
    assert (decision.engine, decision.answers, decision.mode) == ("fake", {}, "shadow")
    assert row.counterfactual_cost_usd == row.cost_usd


async def test_engine_error_is_a_decision_error(tmp_path: Path, upstream: respx.Route) -> None:
    engine = ScriptedEngine(WANTS_OPUS, error=EngineError("scaledown: HTTP 500"))
    async with serving(tmp_path, engine) as s:
        turn_id = await prompt(s.client, "hello", "p1")
        await send(s.client, headers=main_headers("p1"))
        decision = s.repo.get_decision(turn_id)
    assert decision is not None
    assert (decision.rule, decision.chosen_model, decision.desired_model) == ("decision_error", HAIKU, None)


async def test_mode_off_makes_no_decisions(tmp_path: Path, upstream: respx.Route) -> None:
    engine = ScriptedEngine(WANTS_OPUS)
    async with serving(tmp_path, engine, mode=Mode.OFF) as s:
        turn_id = await prompt(s.client, "hello", "p1")
        await send(s.client, headers=main_headers("p1"))
        decision = s.repo.get_decision(turn_id)
        (row,) = s.repo.list_requests(SESSION)
    assert decision is None
    assert engine.calls == []
    assert row.counterfactual_cost_usd is None


async def test_override_prompt_chooses_that_model_and_hides_the_prefix_from_the_engine(
    tmp_path: Path, upstream: respx.Route, book: PriceBook
) -> None:
    engine = ScriptedEngine(WANTS_HAIKU)
    async with serving(tmp_path, engine) as s:
        turn_id = await prompt(s.client, "@opus fix it", "p1")
        await send(s.client, headers=main_headers("p1"))
        decision = s.repo.get_decision(turn_id)
        (row,) = s.repo.list_requests(SESSION)
    assert decision is not None
    assert (decision.rule, decision.chosen_model, decision.desired_model) == ("override", OPUS, HAIKU)
    assert engine.calls[0].prompt == "fix it"
    assert row.counterfactual_cost_usd is not None
    assert row.counterfactual_cost_usd != row.cost_usd
    when = on_date(row.created_at)
    assert row.cost_usd == pytest.approx(cost(book, HAIKU, USAGE, when))
    assert row.counterfactual_cost_usd == pytest.approx(counterfactual(book, OPUS, USAGE, 0, when))


@pytest.mark.parametrize(("idle", "warm_tokens"), [(timedelta(minutes=5), 30000), (timedelta(hours=2), 0)])
async def test_first_request_counterfactual_uses_the_chosen_models_warm_prefix_within_the_ttl(
    tmp_path: Path, upstream: respx.Route, book: PriceBook, idle: timedelta, warm_tokens: int
) -> None:
    async with serving(tmp_path, ScriptedEngine(WANTS_HAIKU)) as s:
        await prompt(s.client, "@opus fix it", "p1")
        s.repo.touch_session_model(
            SessionModel(
                session_id=SESSION,
                model=OPUS,
                last_used_at=(datetime.now(UTC) - idle).isoformat(),
                prefix_tokens=30000,
            )
        )
        await send(s.client, headers=main_headers("p1"))
        (row,) = s.repo.list_requests(SESSION)
    expected = counterfactual(book, OPUS, USAGE, warm_tokens, on_date(row.created_at))
    assert row.counterfactual_cost_usd == pytest.approx(expected)


async def test_decision_event_is_published(tmp_path: Path, upstream: respx.Route) -> None:
    async with serving(tmp_path, ScriptedEngine(WANTS_OPUS)) as s:
        with s.app.state.bus.subscribe() as sub:
            turn_id = await prompt(s.client, "hello", "p1")
            await send(s.client, headers=main_headers("p1"))
            await send(s.client, headers=main_headers("p1"))
            events = drain(sub)
    assert [e.type for e in events] == ["turn", "decision", "request", "request"]
    data = events[1].data
    assert (data["turn_id"], data["rule"], data["chosen_model"]) == (turn_id, "free_window", OPUS)


async def test_decide_endpoint_returns_the_documented_shape(tmp_path: Path) -> None:
    async with serving(tmp_path, ScriptedEngine(WANTS_OPUS)) as s:
        resp = await s.client.post("/api/decide", json={"prompt": "hello", "current_model": SONNET})
        sessions = s.repo._all("SELECT id FROM sessions", ())
    assert resp.status_code == 200
    out = resp.json()
    assert list(out) == [
        "engine",
        "answers",
        "latency_ms",
        "desired_model",
        "current_model",
        "chosen_model",
        "rule",
        "est_switch_tax_usd",
        "est_saving_usd",
        "mode",
    ]
    assert out["engine"] == "fake"
    assert out["answers"] == {"complexity": {"choice": "D", "probabilities": WANTS_OPUS}}
    assert (out["desired_model"], out["current_model"], out["chosen_model"]) == (OPUS, SONNET, OPUS)
    assert (out["rule"], out["mode"], out["latency_ms"]) == ("upgrade", "shadow", 0.0)
    assert sessions == []


async def test_decide_endpoint_honours_an_override_and_reports_engine_errors(tmp_path: Path) -> None:
    async with serving(tmp_path, ScriptedEngine(WANTS_OPUS)) as s:
        resp = await s.client.post("/api/decide", json={"prompt": "hello", "override": "haiku"})
    out = resp.json()
    assert (out["rule"], out["chosen_model"], out["current_model"]) == ("override", HAIKU, None)
    failing = ScriptedEngine(WANTS_OPUS, error=EngineError("boom"))
    async with serving(tmp_path / "again", failing) as s:
        resp = await s.client.post("/api/decide", json={"prompt": "hello"})
    assert resp.status_code == 503
    assert resp.json() == {"error": "boom"}


async def test_scaledown_without_a_key_fails_startup_unless_mode_is_off(tmp_path: Path) -> None:
    app = create_app(shadow_settings(tmp_path / "a.db", engine=Engine.SCALEDOWN))
    with pytest.raises(ConfigError, match="SCALEDOWN_API_KEY"):
        async with app.router.lifespan_context(app):
            pass
    off = create_app(shadow_settings(tmp_path / "b.db", mode=Mode.OFF, engine=Engine.SCALEDOWN))
    async with off.router.lifespan_context(off):
        pass
