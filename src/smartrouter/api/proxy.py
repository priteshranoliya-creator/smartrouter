"""Transparent pass-through to the Anthropic API. Observes responses, never changes a request."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import anyio
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from smartrouter.providers.anthropic import StreamTap
from smartrouter.telemetry.recorder import Exchange, Recorder

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


def _forward_headers(items: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    out = [(k, v) for k, v in items if k.lower() not in NOT_FORWARDED_REQUEST]
    if not any(k.lower() == "accept-encoding" for k, _ in out):
        # httpx would otherwise ask upstream for gzip and we relay raw bytes to a client that never did.
        out.append(("accept-encoding", "identity"))
    return out


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

    def exchange(**fields: Any) -> Exchange:
        return Exchange(
            started_at=started_at,
            method=method,
            path=path,
            request_headers=seen_headers,
            request_model=model,
            metadata_session_id=metadata_session_id,
            latency_ms=(time.perf_counter() - t0) * 1000,
            **fields,
        )

    raw_path = request.scope.get("raw_path") or quote(path).encode()
    query = request.scope["query_string"].decode("latin-1")
    target = raw_path.decode("latin-1") + (f"?{query}" if query else "")
    upstream_request = http.build_request(
        method, target, headers=_forward_headers(request.headers.items()), content=body or None
    )
    try:
        # Active mode (T6.2) awaits the turn's decision here, before forwarding. Shadow mode never waits.
        upstream = await http.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        logger.warning("upstream unreachable for %s %s: %r", method, path, exc)
        await _record(
            recorder,
            exchange(
                status=502,
                usage=None,
                ttft_ms=None,
                error=f"upstream unreachable: {type(exc).__name__}: {exc}",
            ),
        )
        return JSONResponse(UPSTREAM_UNREACHABLE, status_code=502)

    tap = StreamTap(upstream.headers.get("content-type"), upstream.headers.get("content-encoding"))

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
            await _record(
                recorder,
                exchange(
                    status=upstream.status_code,
                    usage=result.usage,
                    ttft_ms=None if first_chunk_at is None else (first_chunk_at - t0) * 1000,
                    error=result.error or aborted,
                ),
            )

    response = StreamingResponse(relay(), status_code=upstream.status_code)
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
