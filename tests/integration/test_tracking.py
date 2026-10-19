import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI

from smartrouter.app import create_app
from smartrouter.settings import ROOT
from smartrouter.telemetry.events import Event
from smartrouter.telemetry.models import Turn
from tests.integration.test_proxy import settings_with_db

FIXTURES = ROOT / "tests" / "fixtures" / "anthropic"
UPSTREAM = "https://api.anthropic.com"
SESSION = "aaaaaaaa-0000-0000-0000-000000000001"
HAIKU = "claude-haiku-5-5"
SSE = {"content-type": "text/event-stream; charset=utf-8"}
TEXT_PREFIX_TOKENS = 2 + 35617


def body(model: str = HAIKU, session: str = SESSION) -> bytes:
    user_id = json.dumps({"session_id": session})
    return json.dumps({"model": model, "stream": True, "metadata": {"user_id": user_id}}).encode()


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    app = create_app(settings_with_db(tmp_path / "router.db"))
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
        yield mock


async def prompt(client: httpx.AsyncClient, text: str, prompt_id: str, session: str = SESSION) -> int:
    resp = await client.post(
        "/hooks/prompt",
        json={"session_id": session, "prompt_id": prompt_id, "prompt": text, "cwd": "/work", "extra": 1},
    )
    assert resp.status_code == 202
    turn_id: int = resp.json()["turn_id"]
    return turn_id


async def send(
    client: httpx.AsyncClient,
    model: str = HAIKU,
    session: str = SESSION,
    headers: dict[str, str] | None = None,
) -> None:
    resp = await client.post(
        "/v1/messages",
        content=body(model, session),
        headers={"content-type": "application/json", **(headers or {})},
    )
    assert resp.status_code == 200
    await resp.aread()


def main_headers(prompt_id: str | None = None, session: str = SESSION) -> dict[str, str]:
    headers = {"x-claude-code-session-id": session, "x-claude-code-request-class": "main"}
    if prompt_id:
        headers["x-claude-code-prompt-id"] = prompt_id
    return headers


def drain(sub: Any) -> list[Event]:
    events: list[Event] = []
    while not sub._queue.empty():
        item = sub._queue.get_nowait()
        if item is not None:
            events.append(item)
    return events


async def test_prompt_then_main_requests_link_to_the_turn(
    client: httpx.AsyncClient, upstream: respx.MockRouter, app: FastAPI
) -> None:
    repo = app.state.repo
    with app.state.bus.subscribe() as sub:
        turn_id = await prompt(client, "fix the bug", "p1")
        await send(client, headers=main_headers("p1"))
        await send(client, headers=main_headers("p1"))
        events = drain(sub)
    session = repo.get_session(SESSION)
    assert session is not None
    assert session.project_path == "/work"
    assert session.current_model == HAIKU
    assert session.state == "active"
    assert session.last_request_at is not None
    (turn,) = repo.list_turns(SESSION)
    assert (turn.id, turn.seq, turn.prompt_id, turn.prompt, turn.override) == (
        turn_id,
        1,
        "p1",
        "fix the bug",
        None,
    )
    requests = repo.list_requests(SESSION)
    assert [(r.turn_id, r.kind) for r in requests] == [(turn_id, "main")] * 2
    (warm,) = repo.get_session_models(SESSION)
    assert (warm.model, warm.prefix_tokens) == (HAIKU, TEXT_PREFIX_TOKENS)
    assert [e.type for e in events] == ["turn", "request", "request"]
    assert events[0].data["id"] == turn_id
    assert events[0].data["prompt"] == "fix the bug"


async def test_second_prompt_gets_next_seq_and_override(
    client: httpx.AsyncClient, upstream: respx.MockRouter, app: FastAPI
) -> None:
    first = await prompt(client, "hello", "p1")
    second = await prompt(client, "@haiku fix it", "p2")
    await send(client, headers=main_headers("p1"))
    await send(client, headers=main_headers("p2"))
    turns = app.state.repo.list_turns(SESSION)
    assert [(t.id, t.seq, t.override) for t in turns] == [(first, 1, None), (second, 2, "haiku")]
    assert [r.turn_id for r in app.state.repo.list_requests(SESSION)] == [first, second]


async def test_precompact_records_a_compaction_and_next_main_request_ends_it(
    client: httpx.AsyncClient, upstream: respx.MockRouter, app: FastAPI
) -> None:
    repo = app.state.repo
    await prompt(client, "hello", "p1")
    await send(client, headers=main_headers("p1"))
    with app.state.bus.subscribe() as sub:
        resp = await client.post("/hooks/precompact", json={"session_id": SESSION, "trigger": "auto"})
        events = drain(sub)
    assert resp.status_code == 202
    (compaction,) = repo.list_compactions(SESSION)
    assert (compaction.trigger, compaction.prefix_tokens_before) == ("auto", TEXT_PREFIX_TOKENS)
    assert resp.json() == {"compaction_id": compaction.id}
    assert [e.type for e in events] == ["compaction"]
    assert events[0].data["prefix_tokens_before"] == TEXT_PREFIX_TOKENS
    session = repo.get_session(SESSION)
    assert session is not None
    assert session.state == "compacting"
    await send(client, headers=main_headers("p1"))
    session = repo.get_session(SESSION)
    assert session is not None
    assert session.state == "active"


async def test_precompact_for_an_unknown_session_has_no_prefix_size(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    resp = await client.post("/hooks/precompact", json={"session_id": "fresh", "trigger": "manual"})
    assert resp.status_code == 202
    (compaction,) = app.state.repo.list_compactions("fresh")
    assert (compaction.trigger, compaction.prefix_tokens_before) == ("manual", None)
    session = app.state.repo.get_session("fresh")
    assert session is not None
    assert (session.project_path, session.state) == ("", "compacting")


async def test_without_hint_headers_the_request_lands_on_the_latest_turn(
    client: httpx.AsyncClient, upstream: respx.MockRouter, app: FastAPI
) -> None:
    await prompt(client, "one", "p1")
    latest = await prompt(client, "two", "p2")
    await send(client)
    (row,) = app.state.repo.list_requests(SESSION)
    assert (row.turn_id, row.kind) == (latest, "main")


async def test_unknown_prompt_id_falls_back_to_the_latest_turn(
    client: httpx.AsyncClient, upstream: respx.MockRouter, app: FastAPI
) -> None:
    latest = await prompt(client, "one", "p1")
    await send(client, headers=main_headers("never-seen"))
    (row,) = app.state.repo.list_requests(SESSION)
    assert row.turn_id == latest


async def test_other_model_without_class_header_is_background(
    client: httpx.AsyncClient, upstream: respx.MockRouter, app: FastAPI
) -> None:
    await prompt(client, "one", "p1")
    await send(client)
    await send(client, model="claude-opus-5-5")
    kinds = [(r.requested_model, r.kind) for r in app.state.repo.list_requests(SESSION)]
    assert kinds == [(HAIKU, "main"), ("claude-opus-5-5", "background")]
    session = app.state.repo.get_session(SESSION)
    assert session is not None
    assert session.current_model == HAIKU
    assert [m.model for m in app.state.repo.get_session_models(SESSION)] == [HAIKU]


async def test_request_for_a_session_without_a_hook_creates_the_session(
    client: httpx.AsyncClient, upstream: respx.MockRouter, app: FastAPI
) -> None:
    await send(client, headers=main_headers("p1"))
    session = app.state.repo.get_session(SESSION)
    assert session is not None
    assert session.project_path == ""
    assert session.current_model == HAIKU
    (row,) = app.state.repo.list_requests(SESSION)
    assert row.turn_id is None
    assert row.kind == "main"


async def test_failed_upstream_response_does_not_move_the_session_model(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    with respx.mock(base_url=UPSTREAM) as mock:
        mock.post("/v1/messages").respond(
            400,
            headers={"content-type": "application/json"},
            content=(FIXTURES / "error_400.json").read_bytes(),
        )
        resp = await client.post("/v1/messages", content=body(), headers={"content-type": "application/json"})
        await resp.aread()
    session = app.state.repo.get_session(SESSION)
    assert session is not None
    assert session.current_model is None
    assert app.state.repo.get_session_models(SESSION) == []


@pytest.mark.parametrize("path", ["/hooks/prompt", "/hooks/precompact"])
@pytest.mark.parametrize(
    "garbage",
    [b"not json", b"[]", b"{}", b'{"session_id": ""}', b'{"session_id": "s", "trigger": "weekly"}'],
)
async def test_garbage_is_a_400_json_error_and_writes_nothing(
    client: httpx.AsyncClient, app: FastAPI, path: str, garbage: bytes
) -> None:
    resp = await client.post(path, content=garbage, headers={"content-type": "application/json"})
    assert resp.status_code == 400
    assert resp.headers["content-type"] == "application/json"
    assert resp.json()["error"].startswith("invalid payload")
    assert app.state.repo.get_session("s") is None
    assert app.state.repo.list_turns("s") == []


async def test_prompt_hook_body_and_turn_row_types(client: httpx.AsyncClient, app: FastAPI) -> None:
    resp = await client.post("/hooks/prompt", json={"session_id": "s2", "prompt": "  @AUTO go"})
    assert resp.status_code == 202
    turn: Turn | None = app.state.repo.get_turn(resp.json()["turn_id"])
    assert turn is not None
    assert (turn.seq, turn.prompt_id, turn.override) == (1, None, "auto")


async def test_hook_events_reach_a_subscriber_waiting_on_the_bus(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    with app.state.bus.subscribe() as sub:
        await prompt(client, "hi", "p1")
        event = await asyncio.wait_for(sub.get(), 1)
    assert event.type == "turn"
