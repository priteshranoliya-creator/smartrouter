"""GET /api/stream against a real uvicorn server, since httpx.ASGITransport buffers whole responses."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from smartrouter.api import stream
from smartrouter.telemetry.events import Event, EventBus


@dataclass(frozen=True)
class Running:
    bus: EventBus
    loop: asyncio.AbstractEventLoop
    url: str


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def running() -> Iterator[Running]:
    bus = EventBus()
    loops: list[asyncio.AbstractEventLoop] = []

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        loops.append(asyncio.get_running_loop())
        yield

    app = FastAPI(lifespan=lifespan)
    app.state.bus = bus
    app.include_router(stream.router)

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not (server.started and loops):
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.01)

    yield Running(bus=bus, loop=loops[0], url=f"http://127.0.0.1:{port}/api/stream")

    server.should_exit = True
    thread.join(timeout=5)


async def _wait_for_subscribers(bus: EventBus, count: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while bus.subscriber_count() < count:
        if time.monotonic() > deadline:
            raise AssertionError(f"expected {count} subscribers, have {bus.subscriber_count()}")
        await asyncio.sleep(0.01)


async def _next_message(lines: AsyncIterator[str]) -> tuple[str, dict[str, object]]:
    event = ""
    data = ""
    async for line in lines:
        if line.startswith("event:"):
            event = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            data = line.removeprefix("data:").strip()
        elif line == "" and event:
            return event, json.loads(data)
    raise AssertionError("stream ended before a complete message")


def _publish(running: Running, event: Event) -> None:
    # The bus is not thread-safe, so hand the publish to the server's own event loop.
    running.loop.call_soon_threadsafe(running.bus.publish, event)


async def test_new_request_row_reaches_client_within_one_second(running: Running) -> None:
    async with httpx.AsyncClient(timeout=10) as client, client.stream("GET", running.url) as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        lines = resp.aiter_lines()
        await _wait_for_subscribers(running.bus, 1, timeout=2)

        start = time.perf_counter()
        _publish(running, Event("request", {"id": 1, "model": "haiku"}))
        event, data = await asyncio.wait_for(_next_message(lines), timeout=1.0)
        elapsed = time.perf_counter() - start

        assert event == "request"
        assert data == {"id": 1, "model": "haiku"}
        print(f"stream delivery latency: {elapsed * 1000:.1f} ms")


async def test_two_clients_both_receive_the_same_event(running: Running) -> None:
    async with (
        httpx.AsyncClient(timeout=10) as client_a,
        httpx.AsyncClient(timeout=10) as client_b,
        client_a.stream("GET", running.url) as resp_a,
        client_b.stream("GET", running.url) as resp_b,
    ):
        lines_a = resp_a.aiter_lines()
        lines_b = resp_b.aiter_lines()
        await _wait_for_subscribers(running.bus, 2, timeout=2)

        _publish(running, Event("decision", {"id": 7}))
        received_a = await asyncio.wait_for(_next_message(lines_a), timeout=1.0)
        received_b = await asyncio.wait_for(_next_message(lines_b), timeout=1.0)

        assert received_a == ("decision", {"id": 7})
        assert received_b == ("decision", {"id": 7})


async def test_disconnect_releases_subscription(running: Running) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        async with client.stream("GET", running.url):
            await _wait_for_subscribers(running.bus, 1, timeout=2)
        # Leaving the block closes the connection; the server must drop the subscription.

        deadline = time.monotonic() + 2
        while running.bus.subscriber_count() != 0:
            if time.monotonic() > deadline:
                raise AssertionError(f"subscriber still registered: {running.bus.subscriber_count()}")
            await asyncio.sleep(0.02)
