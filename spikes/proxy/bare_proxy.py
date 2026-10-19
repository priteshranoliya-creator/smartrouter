# /// script
# requires-python = ">=3.11"
# dependencies = ["starlette==0.48.0", "uvicorn==0.37.0", "httpx[http2]==0.28.1"]
# ///
"""T0.1 / T0.2 spike: a bare logging proxy in front of api.anthropic.com.

    uv run spikes/proxy/bare_proxy.py            # listens on 127.0.0.1:8788

- Forwards every request upstream, streaming the response back byte for byte.
- Logs request headers (credentials redacted), body metadata and SSE usage to out/log.jsonl.
- Accepts hook POSTs on /hooks/<event> and logs them.
- T0.2: if out/override.json holds {"model": "<id>"}, rewrites `model` on main-class requests only.
"""

from __future__ import annotations

import json
import re
import time
import zlib
from pathlib import Path

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

UPSTREAM = "https://api.anthropic.com"
OUT = Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)
LOG = OUT / "log.jsonl"
OVERRIDE = OUT / "override.json"

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "host", "content-length"}
TOKEN_PREFIX = re.compile(r"^(Bearer )?(sk-ant-[a-z]+\d*-)")

client = httpx.AsyncClient(http2=True, timeout=httpx.Timeout(None, connect=30.0))


def log(entry: dict) -> None:
    with LOG.open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def redact(name: str, value: str) -> str:
    if name.lower() not in ("authorization", "x-api-key"):
        return value
    m = TOKEN_PREFIX.match(value)
    return f"<redacted len={len(value)} prefix={m.group(2) if m else '?'}>"


def body_summary(body: dict) -> dict:
    msgs = body.get("messages", [])
    hist_thinking = sum(
        1 for m in msgs if m.get("role") == "assistant" and isinstance(m.get("content"), list)
        for b in m["content"] if b.get("type") in ("thinking", "redacted_thinking"))
    return {
        "model": body.get("model"),
        "stream": body.get("stream"),
        "thinking": body.get("thinking"),
        "metadata": body.get("metadata"),
        "n_messages": len(msgs),
        "history_thinking_blocks": hist_thinking,
        "n_tools": len(body.get("tools", [])),
        "max_tokens": body.get("max_tokens"),
        "top_level_keys": sorted(body.keys()),
    }


class SSETap:
    """Parses a copy of the (possibly gzipped) SSE stream for usage and block types."""

    def __init__(self, encoding: str | None) -> None:
        self.z = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
        self.buf = b""
        self.usage: dict = {}
        self.blocks: list[str] = []
        self.stop_reason = None
        self.error = None
        self.raw_tail = b""

    def feed(self, chunk: bytes) -> None:
        data = self.z.decompress(chunk) if self.z else chunk
        self.buf += data
        self.raw_tail = (self.raw_tail + data)[-2000:]
        while b"\n\n" in self.buf:
            event, self.buf = self.buf.split(b"\n\n", 1)
            for line in event.split(b"\n"):
                if line.startswith(b"data:"):
                    self._data(line[5:].strip())

    def _data(self, raw: bytes) -> None:
        try:
            d = json.loads(raw)
        except ValueError:
            return
        t = d.get("type")
        if t == "message_start":
            self.usage.update(d["message"].get("usage", {}))
        elif t == "content_block_start":
            self.blocks.append(d["content_block"]["type"])
        elif t == "message_delta":
            self.usage.update(d.get("usage", {}))
            self.stop_reason = d.get("delta", {}).get("stop_reason")
        elif t == "error":
            self.error = d.get("error")


async def hook(request: Request) -> Response:
    payload = await request.json()
    log({"ts": time.time(), "kind": "hook", "event": request.path_params["event"],
         "session_id": payload.get("session_id"), "fields": sorted(payload.keys()),
         "trigger": payload.get("trigger"), "prompt_id": payload.get("prompt_id"), "prompt_head": str(payload.get("prompt", ""))[:80]})
    return JSONResponse({"ok": True}, status_code=202)


async def proxy(request: Request) -> Response:
    t0 = time.perf_counter()
    raw = await request.body()
    path = request.url.path
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
    entry: dict = {
        "ts": time.time(), "kind": "request", "method": request.method, "path": path,
        "query": request.url.query,
        "headers": {k: redact(k, v) for k, v in request.headers.items()},
    }

    body_out = raw
    if raw and request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = json.loads(raw)
            entry["body"] = body_summary(body)
            override = json.loads(OVERRIDE.read_text()) if OVERRIDE.exists() else None
            is_main = request.headers.get("x-claude-code-request-class") == "main"
            if override and is_main and path == "/v1/messages" and body.get("model") != override["model"]:
                orig = body["model"]
                needle = json.dumps({"model": orig})[1:-1].encode()  # "model": "x"
                compact = needle.replace(b": ", b":")
                for n in (compact, needle):
                    if n in raw:
                        body_out = raw.replace(n, n.replace(orig.encode(), override["model"].encode()), 1)
                        break
                else:
                    body["model"] = override["model"]
                    body_out = json.dumps(body).encode()
                    entry["rewrite_reserialized"] = True
                entry["rewritten"] = {"from": orig, "to": override["model"]}
        except ValueError:
            entry["body"] = "unparseable"

    url = UPSTREAM + path + (f"?{request.url.query}" if request.url.query else "")
    upstream_req = client.build_request(request.method, url, headers=headers, content=body_out)
    resp = await client.send(upstream_req, stream=True)
    entry["status"] = resp.status_code
    entry["resp_headers"] = {k: v for k, v in resp.headers.items()
                             if k.lower().startswith(("anthropic-", "retry-after", "x-should-retry",
                                                      "request-id", "content-type", "content-encoding"))}
    tap = SSETap(resp.headers.get("content-encoding"))
    first: list[float] = []

    async def relay():
        try:
            async for chunk in resp.aiter_raw():
                if not first:
                    first.append(time.perf_counter())
                try:
                    tap.feed(chunk)
                except Exception as e:  # never let parsing break the relay
                    entry["tap_error"] = repr(e)
                yield chunk
        finally:
            await resp.aclose()
            entry.update({
                "latency_ms": round((time.perf_counter() - t0) * 1000),
                "ttft_ms": round((first[0] - t0) * 1000) if first else None,
                "usage": tap.usage, "blocks": tap.blocks, "stop_reason": tap.stop_reason,
                "sse_error": tap.error,
            })
            if resp.status_code >= 400 or (not tap.usage and path.startswith("/v1/messages")):
                entry["body_tail"] = tap.raw_tail.decode("utf-8", "replace")[-1500:]
            log(entry)
            print(f"{entry['status']} {path} model={entry.get('body', {}).get('model') if isinstance(entry.get('body'), dict) else '-'} "
                  f"class={request.headers.get('x-claude-code-request-class')} "
                  f"rewritten={entry.get('rewritten')} blocks={tap.blocks}", flush=True)

    resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in HOP}
    return StreamingResponse(relay(), status_code=resp.status_code, headers=resp_headers)


app = Starlette(routes=[
    Route("/hooks/{event}", hook, methods=["POST"]),
    Route("/{path:path}", proxy, methods=["GET", "POST", "HEAD", "PUT", "DELETE", "PATCH"]),
])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8788, log_level="warning")
