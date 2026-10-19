import asyncio
import json
import logging
import sqlite3
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI

from smartrouter.app import create_app
from smartrouter.providers.base import Usage
from smartrouter.settings import ROOT, Settings, load_settings
from smartrouter.telemetry.models import Request
from smartrouter.telemetry.pricing import cost, load_pricing

FIXTURES = ROOT / "tests" / "fixtures" / "anthropic"
UPSTREAM = "https://api.anthropic.com"
SESSION = "11111111-2222-3333-4444-555555555555"
SSE = {"content-type": "text/event-stream; charset=utf-8"}
GZIP_SSE = {**SSE, "content-encoding": "gzip"}
BODY = (
    b'{"model": "claude-haiku-5-5",  "stream": true, "metadata": {"user_id": "{\\"session_id\\": \\"%s\\"}"}}'
    % (SESSION.encode())
)
STREAM_NAMES = ["text", "tool_use", "text_after_tool"]


def settings_with_db(db: Path) -> Settings:
    base = load_settings(env={})
    paths = base.router.paths.model_copy(update={"db": db})
    return replace(base, router=base.router.model_copy(update={"paths": paths}))


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "router.db"


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
    with respx.mock(base_url=UPSTREAM, assert_all_called=True) as mock:
        yield mock


async def raw_fetch(
    client: httpx.AsyncClient, method: str, url: str, **kwargs: Any
) -> tuple[httpx.Response, bytes]:
    async with client.stream(method, url, **kwargs) as resp:
        body = b"".join([chunk async for chunk in resp.aiter_raw()])
    return resp, body


def post_headers(**extra: str) -> dict[str, str]:
    return {"content-type": "application/json", "x-claude-code-session-id": SESSION, **extra}


def only_request(app: FastAPI, session: str = SESSION) -> Request:
    (row,) = app.state.repo.list_requests(session)
    return row  # type: ignore[no-any-return]


def row_count(db: Path) -> int:
    return sqlite3.connect(db).execute("select count(*) from requests").fetchone()[0]  # type: ignore[no-any-return]


@pytest.mark.parametrize("name", STREAM_NAMES)
async def test_gzip_streams_relay_byte_identical(
    client: httpx.AsyncClient, upstream: respx.MockRouter, name: str
) -> None:
    recorded = (FIXTURES / f"{name}.sse.gz").read_bytes()
    upstream.post("/v1/messages").respond(200, headers=GZIP_SSE, content=recorded)
    resp, body = await raw_fetch(
        client, "POST", "/v1/messages?beta=true", content=BODY, headers=post_headers()
    )
    assert resp.status_code == 200
    assert body == recorded
    assert resp.headers["content-encoding"] == "gzip"
    assert resp.headers["content-type"] == SSE["content-type"]


@pytest.mark.parametrize("name", STREAM_NAMES)
async def test_identity_streams_relay_byte_identical(
    client: httpx.AsyncClient, upstream: respx.MockRouter, name: str
) -> None:
    recorded = (FIXTURES / f"{name}.sse").read_bytes()
    upstream.post("/v1/messages").respond(200, headers=SSE, content=recorded)
    resp, body = await raw_fetch(client, "POST", "/v1/messages", content=BODY, headers=post_headers())
    assert body == recorded
    assert "content-encoding" not in resp.headers


async def test_non_streaming_json_relays_byte_identical(
    client: httpx.AsyncClient, upstream: respx.MockRouter, app: FastAPI
) -> None:
    recorded = (FIXTURES / "non_streaming.json").read_bytes()
    upstream.post("/v1/messages").respond(200, headers={"content-type": "application/json"}, content=recorded)
    _, body = await raw_fetch(client, "POST", "/v1/messages", content=BODY, headers=post_headers())
    assert body == recorded
    row = only_request(app)
    assert (row.input_tokens, row.cache_write_5m_tokens, row.cache_write_1h_tokens) == (12, 1000, 2000)
    assert (row.cache_read_tokens, row.output_tokens) == (30000, 5)


async def test_request_reaches_upstream_unchanged(
    client: httpx.AsyncClient, upstream: respx.MockRouter
) -> None:
    route = upstream.post("/v1/messages").respond(200, headers=SSE, content=b"")
    headers = post_headers(
        **{
            "anthropic-beta": "oauth-2025-04-20,interleaved-thinking-2025-05-14",
            "anthropic-version": "2023-06-01",
            "authorization": "Bearer sk-ant-oat01-secret",
            "x-api-key": "sk-ant-api03-secret",
            "connection": "close",
            "keep-alive": "timeout=5",
            "accept-encoding": "gzip",
        }
    )
    await raw_fetch(client, "POST", "/v1/messages?beta=true", content=BODY, headers=headers)
    sent = route.calls.last.request
    assert sent.url.query == b"beta=true"
    assert sent.url.path == "/v1/messages"
    assert sent.content == BODY
    for name in (
        "anthropic-beta",
        "anthropic-version",
        "authorization",
        "x-api-key",
        "x-claude-code-session-id",
    ):
        assert sent.headers[name] == headers[name]
    assert sent.headers["host"] == "api.anthropic.com"
    assert sent.headers["connection"] != "close"
    assert "keep-alive" not in sent.headers
    assert sent.headers["accept-encoding"] == "gzip"


async def test_clients_without_accept_encoding_never_get_compressed_upstream(
    client: httpx.AsyncClient, upstream: respx.MockRouter
) -> None:
    route = upstream.post("/v1/messages").respond(200, content=b"{}")
    request = client.build_request("POST", "/v1/messages", content=BODY, headers=post_headers())
    del request.headers["accept-encoding"]
    await client.send(request)
    assert route.calls.last.request.headers["accept-encoding"] == "identity"


async def test_rate_limit_headers_reach_the_client(
    client: httpx.AsyncClient, upstream: respx.MockRouter
) -> None:
    upstream.post("/v1/messages").respond(
        429,
        headers={
            "content-type": "application/json",
            "retry-after": "17",
            "x-should-retry": "true",
            "anthropic-ratelimit-unified-status": "allowed_warning",
            "anthropic-ratelimit-unified-5h-utilization": "0.91",
            "request-id": "req_abc",
        },
        content=b'{"type":"error","error":{"type":"rate_limit_error","message":"slow down"}}',
    )
    resp, _ = await raw_fetch(client, "POST", "/v1/messages", content=BODY, headers=post_headers())
    assert resp.status_code == 429
    assert resp.headers["retry-after"] == "17"
    assert resp.headers["x-should-retry"] == "true"
    assert resp.headers["anthropic-ratelimit-unified-status"] == "allowed_warning"
    assert resp.headers["anthropic-ratelimit-unified-5h-utilization"] == "0.91"
    assert resp.headers["request-id"] == "req_abc"


async def test_400_is_relayed_unmodified_and_recorded(
    client: httpx.AsyncClient, upstream: respx.MockRouter, app: FastAPI
) -> None:
    recorded = (FIXTURES / "error_400.json").read_bytes()
    upstream.post("/v1/messages").respond(400, headers={"content-type": "application/json"}, content=recorded)
    resp, body = await raw_fetch(client, "POST", "/v1/messages", content=BODY, headers=post_headers())
    assert resp.status_code == 400
    assert body == recorded
    row = only_request(app)
    assert row.status == 400
    assert row.error == "model: claude-nope is not a valid model"
    assert row.input_tokens is None


async def test_row_for_a_recorded_stream(
    client: httpx.AsyncClient, upstream: respx.MockRouter, app: FastAPI
) -> None:
    upstream.post("/v1/messages").respond(
        200, headers=GZIP_SSE, content=(FIXTURES / "text.sse.gz").read_bytes()
    )
    bus = app.state.bus
    with bus.subscribe() as events:
        await raw_fetch(
            client,
            "POST",
            "/v1/messages",
            content=BODY,
            headers=post_headers(**{"x-claude-code-request-class": "main"}),
        )
        event = await asyncio.wait_for(events.get(), 1)
    row = only_request(app)
    usage = Usage(input_tokens=2, output_tokens=4, cache_read_tokens=35617, inference_geo="global")
    prices = load_pricing(ROOT / "config/pricing.yaml")
    assert row.session_id == SESSION
    assert row.kind == "main"
    assert (row.requested_model, row.sent_model, row.provider) == (
        "claude-haiku-5-5",
        "claude-haiku-5-5",
        "anthropic",
    )
    assert (row.input_tokens, row.output_tokens, row.cache_read_tokens) == (2, 4, 35617)
    assert (row.cache_write_5m_tokens, row.cache_write_1h_tokens) == (0, 0)
    assert row.cost_usd == cost(
        prices, "claude-haiku-5-5", usage, datetime.fromisoformat(row.created_at).date()
    )
    assert row.cost_usd > 0
    assert row.status == 200
    assert row.latency_ms is not None
    assert row.ttft_ms is not None
    assert row.ttft_ms <= row.latency_ms
    assert event.type == "request"
    assert event.data["session_id"] == SESSION


async def test_session_falls_back_to_metadata_user_id(
    client: httpx.AsyncClient, upstream: respx.MockRouter, app: FastAPI
) -> None:
    upstream.post("/v1/messages").respond(200, headers=SSE, content=(FIXTURES / "text.sse").read_bytes())
    await raw_fetch(
        client, "POST", "/v1/messages", content=BODY, headers={"content-type": "application/json"}
    )
    assert only_request(app).session_id == SESSION


async def test_stream_is_relayed_incrementally(app: FastAPI, upstream: respx.MockRouter) -> None:
    first, second = b"event: ping\ndata: {}\n\n", b'event: ping\ndata: {"n": 2}\n\n'
    release = asyncio.Event()

    async def slow_upstream() -> AsyncIterator[bytes]:
        yield first
        await release.wait()
        yield second

    upstream.post("/v1/messages").mock(return_value=httpx.Response(200, headers=SSE, content=slow_upstream()))

    sent: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    body_read = False

    async def receive() -> dict[str, Any]:
        nonlocal body_read
        if not body_read:
            body_read = True
            return {"type": "http.request", "body": BODY, "more_body": False}
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/messages",
        "raw_path": b"/v1/messages",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"smartrouter.test"), (b"content-type", b"application/json")],
        "client": ("127.0.0.1", 50000),
        "server": ("smartrouter.test", 80),
    }
    task = asyncio.create_task(app(scope, receive, sent.put))  # type: ignore[arg-type]
    try:
        start = await asyncio.wait_for(sent.get(), 5)
        assert start["type"] == "http.response.start"
        chunk = await asyncio.wait_for(sent.get(), 5)
        assert chunk["body"] == first
        assert chunk["more_body"] is True
        release.set()
        chunk = await asyncio.wait_for(sent.get(), 5)
        assert chunk["body"] == second
        await asyncio.wait_for(task, 5)
    finally:
        release.set()
        task.cancel()


async def test_upstream_connection_error_is_a_502(
    client: httpx.AsyncClient, upstream: respx.MockRouter, app: FastAPI
) -> None:
    upstream.post("/v1/messages").mock(side_effect=httpx.ConnectError("refused"))
    resp, body = await raw_fetch(client, "POST", "/v1/messages", content=BODY, headers=post_headers())
    assert resp.status_code == 502
    assert json.loads(body) == {
        "type": "error",
        "error": {"type": "api_error", "message": "smartrouter: upstream unreachable"},
    }
    row = only_request(app)
    assert row.status == 502
    assert row.error is not None
    assert "ConnectError" in row.error


async def test_count_tokens_passes_through_without_a_row(
    client: httpx.AsyncClient, upstream: respx.MockRouter, db_path: Path
) -> None:
    recorded = b'{"input_tokens": 10}'
    route = upstream.post("/v1/messages/count_tokens").respond(
        200, headers={"content-type": "application/json"}, content=recorded
    )
    resp, body = await raw_fetch(
        client, "POST", "/v1/messages/count_tokens", content=BODY, headers=post_headers()
    )
    assert (resp.status_code, body) == (200, recorded)
    assert route.calls.last.request.content == BODY
    assert row_count(db_path) == 0


async def test_unknown_path_passes_through_with_a_warning(
    client: httpx.AsyncClient, upstream: respx.MockRouter, caplog: pytest.LogCaptureFixture, db_path: Path
) -> None:
    upstream.head("/api/hello").respond(200, headers={"x-hello": "yes"})
    with caplog.at_level(logging.WARNING, logger="smartrouter.api.proxy"):
        resp, body = await raw_fetch(client, "HEAD", "/api/hello")
    assert resp.status_code == 200
    assert resp.headers["x-hello"] == "yes"
    assert body == b""
    assert any("/api/hello" in r.getMessage() for r in caplog.records)
    assert row_count(db_path) == 0


async def test_recording_failure_never_breaks_the_client(
    client: httpx.AsyncClient, upstream: respx.MockRouter, app: FastAPI
) -> None:
    def boom(_: object) -> int:
        raise RuntimeError("disk full")

    app.state.recorder.record = boom
    recorded = (FIXTURES / "text.sse").read_bytes()
    upstream.post("/v1/messages").respond(200, headers=SSE, content=recorded)
    _, body = await raw_fetch(client, "POST", "/v1/messages", content=BODY, headers=post_headers())
    assert body == recorded
