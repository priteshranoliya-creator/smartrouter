"""T6.2 active mode: main requests are rewritten to the turn's chosen model without waiting for the
engine (tdd-changes #28), with one fallback retry on the asked model."""

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI

from smartrouter.app import create_app
from smartrouter.settings import ConfigError, Mode, Settings
from smartrouter.telemetry.pricing import PriceBook, cost, counterfactual
from tests.integration.test_shadow import (
    OPUS,
    SONNET,
    USAGE,
    WANTS_OPUS,
    ScriptedEngine,
    Served,
    book,
    on_date,
    shadow_settings,
)
from tests.integration.test_tracking import (
    FIXTURES,
    HAIKU,
    SESSION,
    SSE,
    UPSTREAM,
    body,
    main_headers,
    prompt,
    send,
)

__all__ = ["book"]

TEXT = (FIXTURES / "text.sse").read_bytes()
ERROR_400 = (FIXTURES / "error_400.json").read_bytes()


def active_settings(db: Path, allow_active: bool = True) -> Settings:
    base = shadow_settings(db, mode=Mode.ACTIVE)
    return replace(base, router=base.router.model_copy(update={"allow_active": allow_active}))


@asynccontextmanager
async def serving(tmp_path: Path, engine: ScriptedEngine, allow_active: bool = True) -> AsyncIterator[Served]:
    app = create_app(active_settings(tmp_path / "router.db", allow_active), engine)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://smartrouter.test"
        ) as client,
    ):
        yield Served(app, client)


def sent_model(request: httpx.Request) -> str:
    model: str = json.loads(request.content)["model"]
    return model


class Upstream:
    """Answers every model with text.sse, except `failing` models, which get a 400."""

    def __init__(self, mock: respx.MockRouter) -> None:
        self.failing: set[str] = set()
        self.unreachable: set[str] = set()
        self.on_call: Any = None
        self.route = mock.post("/v1/messages").mock(side_effect=self._answer)

    def _answer(self, request: httpx.Request) -> httpx.Response:
        if self.on_call is not None:
            self.on_call()
        if sent_model(request) in self.unreachable:
            raise httpx.ConnectError("down", request=request)
        if sent_model(request) in self.failing:
            return httpx.Response(400, headers={"content-type": "application/json"}, content=ERROR_400)
        return httpx.Response(200, headers=SSE, content=TEXT)

    @property
    def models(self) -> list[str]:
        return [sent_model(call.request) for call in self.route.calls]


@pytest.fixture
def upstream() -> Iterator[Upstream]:
    with respx.mock(base_url=UPSTREAM, assert_all_called=False) as mock:
        yield Upstream(mock)


async def decided(s: Served, turn_id: int) -> None:
    assert await s.app.state.decisions.outcome(turn_id) is not None


async def test_first_request_never_waits_and_the_next_request_of_the_turn_switches(
    tmp_path: Path, upstream: Upstream
) -> None:
    gate = asyncio.Event()
    upstream.on_call = gate.set  # the engine answers only once the first request has gone out
    async with serving(tmp_path, ScriptedEngine(WANTS_OPUS, gate=gate)) as s:
        turn_id = await prompt(s.client, "design the cache layer", "p1")
        await send(s.client, headers=main_headers("p1"))
        await send(s.client, headers=main_headers("p1"))
        decision = s.repo.get_decision(turn_id)
        first, second = s.repo.list_requests(SESSION)
        session = s.repo.get_session(SESSION)
    assert upstream.models == [HAIKU, OPUS]
    assert decision is not None
    assert (decision.mode, decision.rule, decision.chosen_model) == ("active", "free_window", OPUS)
    assert (first.requested_model, first.sent_model) == (HAIKU, HAIKU)
    assert (second.requested_model, second.sent_model) == (HAIKU, OPUS)
    assert (session.current_model, session.last_switch_turn) == (OPUS, 1)


async def test_a_decision_ready_before_the_first_request_applies_to_it(
    tmp_path: Path, upstream: Upstream
) -> None:
    async with serving(tmp_path, ScriptedEngine(WANTS_OPUS)) as s:
        turn_id = await prompt(s.client, "design the cache layer", "p1")
        await decided(s, turn_id)
        await send(s.client, headers=main_headers("p1"))
    assert upstream.models == [OPUS]


async def test_a_new_turn_starts_on_the_model_the_session_runs_now(
    tmp_path: Path, upstream: Upstream
) -> None:
    gate = asyncio.Event()
    async with serving(tmp_path, ScriptedEngine(WANTS_OPUS, gate=gate)) as s:
        gate.set()
        first = await prompt(s.client, "design the cache layer", "p1")
        await decided(s, first)
        await send(s.client, headers=main_headers("p1"))  # switched to opus
        gate.clear()
        upstream.on_call = gate.set
        await prompt(s.client, "and now the tests", "p2")
        await send(s.client, headers=main_headers("p2"))  # Claude Code still asks for haiku
    assert upstream.models == [OPUS, OPUS]


async def test_an_override_applies_to_the_first_request(tmp_path: Path, upstream: Upstream) -> None:
    gate = asyncio.Event()
    upstream.on_call = gate.set
    async with serving(tmp_path, ScriptedEngine(WANTS_OPUS, gate=gate)) as s:
        await prompt(s.client, "@sonnet rename this variable", "p1")
        await send(s.client, headers=main_headers("p1"))
    assert upstream.models == [SONNET]


async def test_a_4xx_on_the_routed_model_is_retried_once_on_the_asked_model(
    tmp_path: Path, upstream: Upstream
) -> None:
    upstream.failing = {OPUS}
    async with serving(tmp_path, ScriptedEngine(WANTS_OPUS)) as s:
        turn_id = await prompt(s.client, "design the cache layer", "p1")
        await decided(s, turn_id)
        await send(s.client, headers=main_headers("p1"))  # send asserts the client got a 200
        await send(s.client, headers=main_headers("p1"))
        first, second = s.repo.list_requests(SESSION)
        session = s.repo.get_session(SESSION)
    assert upstream.models == [OPUS, HAIKU, HAIKU]  # the turn does not try opus again
    assert (first.requested_model, first.sent_model, first.status) == (HAIKU, HAIKU, 200)
    assert first.error == f"fallback: {OPUS} returned 400, retried on {HAIKU}"
    assert (second.sent_model, second.error) == (HAIKU, None)
    assert session.current_model == HAIKU


async def test_a_4xx_on_the_asked_model_is_not_retried(tmp_path: Path, upstream: Upstream) -> None:
    upstream.failing = {HAIKU}
    async with serving(tmp_path, ScriptedEngine(WANTS_OPUS)) as s:
        await prompt(s.client, "hello", "p1")
        resp = await s.client.post(
            "/v1/messages",
            content=json.dumps({"model": HAIKU, "stream": True}).encode(),
            headers={"content-type": "application/json", **main_headers("p1")},
        )
    assert resp.status_code == 400
    assert upstream.models == [HAIKU]


@pytest.mark.parametrize("request_class", ["auxiliary", "subagent", "compaction"])
async def test_only_main_requests_are_rewritten(
    tmp_path: Path, upstream: Upstream, request_class: str
) -> None:
    async with serving(tmp_path, ScriptedEngine(WANTS_OPUS)) as s:
        turn_id = await prompt(s.client, "design the cache layer", "p1")
        await decided(s, turn_id)
        await send(s.client, headers=main_headers("p1"))
        await send(s.client, headers={**main_headers("p1"), "x-claude-code-request-class": request_class})
    assert upstream.models == [OPUS, HAIKU]


async def test_a_model_outside_the_tiers_is_never_rewritten(tmp_path: Path, upstream: Upstream) -> None:
    async with serving(tmp_path, ScriptedEngine(WANTS_OPUS)) as s:
        turn_id = await prompt(s.client, "design the cache layer", "p1")
        await decided(s, turn_id)
        await send(s.client, model="claude-other-1", headers=main_headers("p1"))
    assert upstream.models == ["claude-other-1"]


async def test_routed_request_costs_what_ran_and_the_counterfactual_is_the_asked_model(
    tmp_path: Path, upstream: Upstream, book: PriceBook
) -> None:
    async with serving(tmp_path, ScriptedEngine(WANTS_OPUS)) as s:
        turn_id = await prompt(s.client, "design the cache layer", "p1")
        await decided(s, turn_id)
        await send(s.client, headers=main_headers("p1"))
        await send(s.client, headers=main_headers("p1"))
        first, second = s.repo.list_requests(SESSION)
    when = on_date(second.created_at)
    assert first.cost_usd == pytest.approx(cost(book, OPUS, USAGE, when))
    assert first.counterfactual_cost_usd == pytest.approx(counterfactual(book, HAIKU, USAGE, 0, when))
    assert second.counterfactual_cost_usd == pytest.approx(counterfactual(book, HAIKU, USAGE, 35617, when))


async def test_every_request_logs_the_time_the_proxy_held_it(tmp_path: Path, upstream: Upstream) -> None:
    async with serving(tmp_path, ScriptedEngine(WANTS_OPUS)) as s:
        await prompt(s.client, "hello", "p1")
        await send(s.client, headers=main_headers("p1"))
        (row,) = s.repo.list_requests(SESSION)
    assert row.overhead_ms is not None
    assert 0 <= row.overhead_ms < row.latency_ms


async def test_active_mode_does_not_start_without_allow_active(tmp_path: Path) -> None:
    app = create_app(active_settings(tmp_path / "router.db", allow_active=False), ScriptedEngine(WANTS_OPUS))
    with pytest.raises(ConfigError, match="allow_active"):
        async with app.router.lifespan_context(app):
            pass


async def test_mode_switch_to_active_needs_allow_active(tmp_path: Path) -> None:
    for allow, status, mode in [(False, 409, "shadow"), (True, 200, "active")]:
        settings = shadow_settings(tmp_path / f"{allow}.db")
        settings = replace(settings, router=settings.router.model_copy(update={"allow_active": allow}))
        app: FastAPI = create_app(settings, ScriptedEngine(WANTS_OPUS))
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client,
        ):
            resp = await client.put("/api/mode", json={"mode": "active"})
            now = (await client.get("/api/mode")).json()["mode"]
        assert (resp.status_code, now) == (status, mode)


async def test_a_routing_failure_forwards_the_request_as_asked(
    tmp_path: Path, upstream: Upstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with serving(tmp_path, ScriptedEngine(WANTS_OPUS)) as s:
        turn_id = await prompt(s.client, "design the cache layer", "p1")
        await decided(s, turn_id)

        def broken(*args: object) -> None:
            raise RuntimeError("db locked")

        monkeypatch.setattr(s.app.state.recorder, "route", broken)
        await send(s.client, headers=main_headers("p1"))
    assert upstream.models == [HAIKU]


async def asgi_post(app: FastAPI, payload: bytes, headers: dict[str, str], body_done: asyncio.Event) -> None:
    """One POST /v1/messages straight through ASGI, setting `body_done` when the last body chunk is sent."""
    messages = iter([{"type": "http.request", "body": payload, "more_body": False}])
    disconnected = asyncio.Event()

    async def receive() -> dict[str, Any]:
        message = next(messages, None)
        if message is None:
            await disconnected.wait()
            return {"type": "http.disconnect"}
        return message

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            body_done.set()

    raw = [(k.encode(), v.encode()) for k, v in {"content-type": "application/json", **headers}.items()]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/messages",
        "raw_path": b"/v1/messages",
        "query_string": b"",
        "headers": raw,
        "server": ("127.0.0.1", 8787),
        "client": ("127.0.0.1", 50000),
        "root_path": "",
    }
    try:
        await app(scope, receive, send)
    finally:
        disconnected.set()


@pytest.mark.parametrize("mode", [Mode.SHADOW, Mode.ACTIVE])
async def test_the_response_ends_before_the_decision_is_recorded(
    tmp_path: Path, upstream: Upstream, mode: Mode
) -> None:
    """Recording waits for the turn's decision; the client must not (TDD section 1, tdd-changes #28)."""
    gate = asyncio.Event()
    engine = ScriptedEngine(WANTS_OPUS, gate=gate)
    settings = active_settings(tmp_path / "router.db")
    settings = replace(settings, router=settings.router.model_copy(update={"mode": mode}))
    app = create_app(settings, engine)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client,
    ):
        turn_id = await prompt(client, "design the cache layer", "p1")
        body_done = asyncio.Event()
        call = asyncio.create_task(asgi_post(app, body(), main_headers("p1"), body_done))
        await asyncio.wait_for(body_done.wait(), 1.0)
        assert app.state.repo.get_decision(turn_id) is None  # still waiting on the engine
        gate.set()
        await asyncio.wait_for(call, 5.0)
        assert app.state.repo.get_decision(turn_id) is not None
        assert len(app.state.repo.list_requests(SESSION)) == 1


async def test_a_turn_that_fell_back_stays_as_asked_after_a_restart(
    tmp_path: Path, upstream: Upstream
) -> None:
    upstream.failing = {OPUS}
    async with serving(tmp_path, ScriptedEngine(WANTS_OPUS)) as s:
        turn_id = await prompt(s.client, "design the cache layer", "p1")
        await decided(s, turn_id)
        await send(s.client, headers=main_headers("p1"))
    async with serving(tmp_path, ScriptedEngine(WANTS_OPUS)) as s:
        await send(s.client, headers=main_headers("p1"))
    assert upstream.models == [OPUS, HAIKU, HAIKU]


async def test_a_switch_that_fell_back_does_not_start_the_cooldown(
    tmp_path: Path, upstream: Upstream
) -> None:
    upstream.failing = {OPUS}
    async with serving(tmp_path, ScriptedEngine(WANTS_OPUS)) as s:
        turn_id = await prompt(s.client, "design the cache layer", "p1")
        await decided(s, turn_id)
        await send(s.client, headers=main_headers("p1"))
        session = s.repo.get_session(SESSION)
    assert (session.current_model, session.last_switch_turn) == (HAIKU, None)


async def test_shadow_leaves_the_request_alone_even_with_a_decision_ready(
    tmp_path: Path, upstream: Upstream
) -> None:
    settings = active_settings(tmp_path / "router.db")
    settings = replace(settings, router=settings.router.model_copy(update={"mode": Mode.SHADOW}))
    app = create_app(settings, ScriptedEngine(WANTS_OPUS))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client,
    ):
        turn_id = await prompt(client, "design the cache layer", "p1")
        assert await app.state.decisions.outcome(turn_id) is not None
        await send(client, headers=main_headers("p1"))
        await send(client, headers=main_headers("p1"))
        session = app.state.repo.get_session(SESSION)
    assert [call.request.content for call in upstream.route.calls] == [body(), body()]
    assert (session.current_model, session.last_switch_turn) == (HAIKU, None)


async def test_a_fallback_whose_retry_fails_still_marks_the_turn(tmp_path: Path, upstream: Upstream) -> None:
    upstream.failing, upstream.unreachable = {OPUS}, {HAIKU}
    async with serving(tmp_path, ScriptedEngine(WANTS_OPUS)) as s:
        turn_id = await prompt(s.client, "design the cache layer", "p1")
        await decided(s, turn_id)
        resp = await s.client.post(
            "/v1/messages",
            content=body(),
            headers={"content-type": "application/json", **main_headers("p1")},
        )
        assert resp.status_code == 502
    upstream.unreachable = set()
    async with serving(tmp_path, ScriptedEngine(WANTS_OPUS)) as s:
        await send(s.client, headers=main_headers("p1"))
        failed, after = s.repo.list_requests(SESSION)
    assert failed.error.startswith(f"fallback: {OPUS} returned 400")
    assert after.sent_model == HAIKU


async def test_a_fallback_to_a_third_model_does_not_start_the_cooldown(
    tmp_path: Path, upstream: Upstream
) -> None:
    upstream.failing = {OPUS}
    async with serving(tmp_path, ScriptedEngine(WANTS_OPUS)) as s:
        await prompt(s.client, "@sonnet rename this", "p1")
        await send(s.client, headers=main_headers("p1"))  # session now on sonnet
        turn_id = await prompt(s.client, "design the cache layer", "p2")
        await decided(s, turn_id)
        await send(s.client, headers=main_headers("p2"))  # opus refused, retried on haiku
        session = s.repo.get_session(SESSION)
    assert upstream.models == [SONNET, OPUS, HAIKU]
    assert (session.current_model, session.last_switch_turn) == (HAIKU, None)
