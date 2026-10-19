"""GET /api/stream: server-sent events, one message per telemetry event published on app.state.bus."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from smartrouter.telemetry.events import EventBus, Subscription

router = APIRouter()

PING_SECONDS = 15


@router.get("/api/stream")
async def stream(request: Request) -> EventSourceResponse:
    # Subscribe before the response starts so no event published during the handshake is missed.
    bus: EventBus = request.app.state.bus
    return EventSourceResponse(_messages(bus.subscribe()), ping=PING_SECONDS)


async def _messages(sub: Subscription) -> AsyncIterator[dict[str, str]]:
    try:
        async for event in sub:
            yield {"event": event.type, "data": json.dumps(event.data, default=str)}
    finally:
        sub.close()
