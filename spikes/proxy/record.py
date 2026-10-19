# /// script
# requires-python = ">=3.11"
# dependencies = ["starlette==0.48.0", "uvicorn==0.37.0", "httpx[http2]==0.28.1"]
# ///
"""Record raw upstream responses for the proxy tests (T2.2).

    uv run spikes/proxy/record.py <out-dir>     # listens on 127.0.0.1:8788

For each POST /v1/messages it writes <n>.body (raw bytes as received, still encoded)
and <n>.json (status, content-type, content-encoding, request model/stream). Request
bodies and identifying response headers are never saved: the fixtures are public.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
HOP = {"connection", "keep-alive", "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}
client = httpx.AsyncClient(http2=True, timeout=httpx.Timeout(None, connect=30.0))
counter = [0]


async def proxy(request: Request) -> Response:
    raw = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
    url = "https://api.anthropic.com" + request.url.path + (f"?{request.url.query}" if request.url.query else "")
    resp = await client.send(client.build_request(request.method, url, headers=headers, content=raw), stream=True)
    record = request.url.path == "/v1/messages"
    if record:
        counter[0] += 1
        n = counter[0]
        body = json.loads(raw)
        meta = {
            "status": resp.status_code,
            "content_type": resp.headers.get("content-type"),
            "content_encoding": resp.headers.get("content-encoding"),
            "request_model": body.get("model"),
            "request_stream": body.get("stream"),
            "request_class": request.headers.get("x-claude-code-request-class"),
        }
        (OUT / f"{n:02d}.json").write_text(json.dumps(meta, indent=2) + "\n")
    chunks: list[bytes] = []

    async def relay():
        try:
            async for chunk in resp.aiter_raw():
                chunks.append(chunk)
                yield chunk
        finally:
            await resp.aclose()
            if record:
                (OUT / f"{n:02d}.body").write_bytes(b"".join(chunks))

    out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in HOP}
    return StreamingResponse(relay(), status_code=resp.status_code, headers=out_headers)


async def hook(request: Request) -> Response:
    return Response(status_code=202)  # keep hook payloads local; never forward them upstream


app = Starlette(
    routes=[
        Route("/hooks/{event}", hook, methods=["POST"]),
        Route("/{path:path}", proxy, methods=["GET", "POST", "HEAD"]),
    ]
)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8788, log_level="warning")
