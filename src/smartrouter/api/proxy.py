"""Pass-through to the Anthropic API. Observes responses; in active mode it rewrites the model of
main requests (T6.2) and retries once on the asked model if the routed one is refused."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import anyio
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from smartrouter.providers.anthropic import StreamTap
from smartrouter.telemetry.recorder import Exchange, Recorder, Route

logger = logging.getLogger(__name__)

router = APIRouter()

HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)
NOT_FORWARDED_REQUEST = HOP_BY_HOP | {"host", "content-length"}
NOT_FORWARDED_RESPONSE = HOP_BY_HOP | {"content-length"}
CREDENTIAL_HEADERS = frozenset({"authorization", "x-api-key"})
METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

# A 4xx the model change can cause (the routed model rejects a parameter, or does not exist).
FALLBACK_STATUSES = frozenset({400, 404})

UPSTREAM_UNREACHABLE = {
    "type": "error",
    "error": {"type": "api_error", "message": "smartrouter: upstream unreachable"},
}


def _request_facts(body: bytes) -> tuple[str | None, str | None]:
    """The request's model and the session id inside metadata.user_id, from a copy of the body."""
    try:
        data = json.loads(body)
    except ValueError:
        return None, None
    if not isinstance(data, dict):
        return None, None
    model = data.get("model")
    metadata = data.get("metadata")
    user_id = metadata.get("user_id") if isinstance(metadata, dict) else None
    session_id: Any = None
    if isinstance(user_id, str):
        try:
            inner = json.loads(user_id)
        except ValueError:
            inner = None
        session_id = inner.get("session_id") if isinstance(inner, dict) else None
    return (
        model if isinstance(model, str) else None,
        session_id if isinstance(session_id, str) else None,
    )


def _with_model(body: bytes, model: str) -> bytes:
    data = json.loads(body)
    data["model"] = model
    return json.dumps(data, ensure_ascii=False).encode()


def _forward_headers(items: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    out = [(k, v) for k, v in items if k.lower() not in NOT_FORWARDED_REQUEST]
    if not any(k.lower() == "accept-encoding" for k, _ in out):
        # httpx would otherwise ask upstream for gzip and we relay raw bytes to a client that never did.
        out.append(("accept-encoding", "identity"))
    return out


def _route(
    recorder: Recorder, headers: Mapping[str, str], model: str | None, metadata_session_id: str | None
) -> Route | None:
    """The route to apply, None to forward as asked. Routing never stops a request from going out."""
    try:
        route = recorder.route(headers, model, metadata_session_id)
    except Exception:
        logger.exception("routing failed; forwarding as asked")
        return None
    return route if route is not None and route.apply else None


async def _record(recorder: Recorder, exchange: Exchange) -> None:
    """Shielded: a client that hung up still gets its exchange (and the turn's decision) recorded."""
    with anyio.CancelScope(shield=True):
        try:
            await recorder.arecord(exchange)
        except Exception:
            logger.exception("recording %s %s failed", exchange.method, exchange.path)


async def _relay(request: Request) -> Response:
    started_at = datetime.now(UTC)
    t0 = time.perf_counter()
    http: httpx.AsyncClient = request.app.state.http
    recorder: Recorder = request.app.state.recorder
    method, path = request.method, request.url.path
    body = await request.body()
    is_messages = method == "POST" and path == "/v1/messages"
    model, metadata_session_id = _request_facts(body) if is_messages else (None, None)
    seen_headers = {k.lower(): v for k, v in request.headers.items() if k.lower() not in CREDENTIAL_HEADERS}

    route = _route(recorder, seen_headers, model, metadata_session_id) if is_messages else None
    sent_model = model if route is None else route.model
    note: str | None = None

    def exchange(**fields: Any) -> Exchange:
        return Exchange(
            started_at=started_at,
            method=method,
            path=path,
            request_headers=seen_headers,
            request_model=model,
            metadata_session_id=metadata_session_id,
            sent_model=sent_model,
            fell_back=note is not None,
            overhead_ms=overhead_ms,
            latency_ms=(time.perf_counter() - t0) * 1000,
            **fields,
        )

    raw_path = request.scope.get("raw_path") or quote(path).encode()
    query = request.scope["query_string"].decode("latin-1")
    target = raw_path.decode("latin-1") + (f"?{query}" if query else "")
    headers = _forward_headers(request.headers.items())
    content = body if route is None else _with_model(body, route.model)
    overhead_ms = (time.perf_counter() - t0) * 1000
    try:
        upstream = await http.send(
            http.build_request(method, target, headers=headers, content=content or None), stream=True
        )
        if route is not None and upstream.status_code in FALLBACK_STATUSES:
            await upstream.aclose()
            logger.warning(
                "%s returned %s; retrying turn %s on %s",
                route.model,
                upstream.status_code,
                route.turn_id,
                model,
            )
            note = f"fallback: {route.model} returned {upstream.status_code}, retried on {model}"
            recorder.fell_back(route.turn_id)
            sent_model = model
            upstream = await http.send(
                http.build_request(method, target, headers=headers, content=body), stream=True
            )
    except httpx.HTTPError as exc:
        logger.warning("upstream unreachable for %s %s: %r", method, path, exc)
        failed = exchange(
            status=502,
            usage=None,
            ttft_ms=None,
            error="; ".join(filter(None, [note, f"upstream unreachable: {type(exc).__name__}: {exc}"])),
        )
        return JSONResponse(
            UPSTREAM_UNREACHABLE, status_code=502, background=BackgroundTask(_record, recorder, failed)
        )

    tap = StreamTap(upstream.headers.get("content-type"), upstream.headers.get("content-encoding"))

    finished: list[Exchange] = []

    async def relay() -> AsyncIterator[bytes]:
        first_chunk_at: float | None = None
        aborted: str | None = None
        try:
            async for chunk in upstream.aiter_raw():
                if first_chunk_at is None:
                    first_chunk_at = time.perf_counter()
                tap.feed(chunk)
                yield chunk
        except BaseException as exc:
            aborted = f"stream aborted: {type(exc).__name__}"
            raise
        finally:
            with anyio.CancelScope(shield=True):
                await upstream.aclose()
            result = tap.finish()
            done = exchange(
                status=upstream.status_code,
                usage=result.usage,
                ttft_ms=None if first_chunk_at is None else (first_chunk_at - t0) * 1000,
                error="; ".join(filter(None, [note, result.error or aborted])) or None,
            )
            if aborted:
                await _record(recorder, done)  # no response left to hold up, and no background task runs
            else:
                finished.append(done)

    async def record_finished() -> None:
        for done in finished:
            await _record(recorder, done)

    # Recording awaits the turn's decision, so it runs after the last byte has gone to the client.
    response = StreamingResponse(
        relay(), status_code=upstream.status_code, background=BackgroundTask(record_finished)
    )
    response.raw_headers = [
        (k.lower(), v)
        for k, v in upstream.headers.raw
        if k.decode("latin-1").lower() not in NOT_FORWARDED_RESPONSE
    ]
    return response


@router.post("/v1/messages")
async def messages(request: Request) -> Response:
    return await _relay(request)


@router.post("/v1/messages/count_tokens")
async def count_tokens(request: Request) -> Response:
    return await _relay(request)


@router.api_route("/{path:path}", methods=METHODS)
async def passthrough(request: Request) -> Response:
    logger.warning("passing through unrecognized path %s %s", request.method, request.url.path)
    return await _relay(request)
