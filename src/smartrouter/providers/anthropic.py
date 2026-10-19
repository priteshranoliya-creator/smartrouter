"""Anthropic Messages API: usage parsing for streaming and non-streaming responses. Pure, no I/O."""

from __future__ import annotations

import json
import zlib
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from typing import Any

from smartrouter.providers.base import AnthropicRequest, Usage

MAX_BUFFER_BYTES = 16 * 1024 * 1024
_KEPT_EVENTS = frozenset({"message_start", "message_delta", "error"})


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def usage_from_dict(u: dict[str, Any]) -> Usage:
    split = u.get("cache_creation")
    if isinstance(split, dict) and (
        "ephemeral_5m_input_tokens" in split or "ephemeral_1h_input_tokens" in split
    ):
        write_5m = _count(split.get("ephemeral_5m_input_tokens"))
        write_1h = _count(split.get("ephemeral_1h_input_tokens"))
    else:
        write_5m, write_1h = _count(u.get("cache_creation_input_tokens")), 0
    geo = u.get("inference_geo")
    return Usage(
        input_tokens=_count(u.get("input_tokens")),
        output_tokens=_count(u.get("output_tokens")),
        cache_read_tokens=_count(u.get("cache_read_input_tokens")),
        cache_write_5m_tokens=write_5m,
        cache_write_1h_tokens=write_1h,
        inference_geo=geo if isinstance(geo, str) else None,
    )


def usage_from_events(events: list[dict[str, Any]]) -> Usage:
    """Merge message_start usage with each message_delta usage; later non-null values win."""
    merged: dict[str, Any] = {}
    for event in events:
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message")
            part = message.get("usage") if isinstance(message, dict) else None
        elif kind == "message_delta":
            part = event.get("usage")
        else:
            continue
        if isinstance(part, dict):
            merged.update({k: v for k, v in part.items() if v is not None})
    return usage_from_dict(merged)


def usage_from_message(body: dict[str, Any]) -> Usage:
    usage = body.get("usage")
    return usage_from_dict(usage if isinstance(usage, dict) else {})


@dataclass(frozen=True)
class TapResult:
    usage: Usage | None
    error: str | None
    stop_reason: str | None


def _error_message(error: Any) -> str | None:
    if not isinstance(error, dict):
        return None
    message = error.get("message")
    return message if isinstance(message, str) else str(error.get("type") or "unknown error")


class StreamTap:
    """Observes a response as it is relayed. feed() never raises; a failure only disables parsing."""

    def __init__(self, content_type: str | None, content_encoding: str | None) -> None:
        media = (content_type or "").lower()
        self._sse = "text/event-stream" in media
        self._json = "application/json" in media
        encoding = (content_encoding or "identity").strip().lower()
        self._inflater: Any = None
        self._broken: str | None = None
        if encoding in ("gzip", "x-gzip"):
            self._inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif encoding == "deflate":
            self._inflater = zlib.decompressobj(zlib.MAX_WBITS)
        elif encoding not in ("identity", ""):
            self._broken = f"unsupported content-encoding {encoding}"
        self._buf = b""
        self._events: list[dict[str, Any]] = []

    def feed(self, chunk: bytes) -> None:
        if self._broken or not (self._sse or self._json):
            return
        try:
            data = self._inflater.decompress(chunk) if self._inflater else chunk
            self._buf += data
            if self._sse:
                self._drain_events()
            if len(self._buf) > MAX_BUFFER_BYTES:
                self._broken = "response too large to parse"
        except Exception as exc:
            self._broken = f"{type(exc).__name__}: {exc}"

    def _drain_events(self) -> None:
        self._buf = self._buf.replace(b"\r\n", b"\n")
        while (end := self._buf.find(b"\n\n")) != -1:
            block, self._buf = self._buf[:end], self._buf[end + 2 :]
            self._parse_block(block)

    def _parse_block(self, block: bytes) -> None:
        lines = [line[5:].removeprefix(b" ") for line in block.split(b"\n") if line.startswith(b"data:")]
        if not lines:
            return
        try:
            event = json.loads(b"\n".join(lines))
        except ValueError:
            return
        if isinstance(event, dict) and event.get("type") in _KEPT_EVENTS:
            self._events.append(event)

    def finish(self) -> TapResult:
        try:
            return self._result()
        except Exception as exc:
            return TapResult(None, f"usage parse failed: {type(exc).__name__}: {exc}", None)

    def _result(self) -> TapResult:
        if self._broken:
            return TapResult(None, f"usage parse failed: {self._broken}", None)
        if self._sse:
            if self._buf.strip():
                self._parse_block(self._buf)
            return self._from_events()
        if self._json and self._buf.strip():
            return self._from_body(json.loads(self._buf))
        return TapResult(None, None, None)

    def _from_events(self) -> TapResult:
        error = next((_error_message(e.get("error")) for e in self._events if e["type"] == "error"), None)
        stop_reason: str | None = None
        for event in self._events:
            delta = event.get("delta")
            if event["type"] == "message_delta" and isinstance(delta, dict):
                stop_reason = delta.get("stop_reason") or stop_reason
        has_usage = any(e["type"] != "error" for e in self._events)
        usage = usage_from_events(self._events) if has_usage else None
        return TapResult(usage, error, stop_reason)

    def _from_body(self, body: Any) -> TapResult:
        if not isinstance(body, dict):
            return TapResult(None, None, None)
        if body.get("type") == "error" or "error" in body:
            return TapResult(None, _error_message(body.get("error")), None)
        usage = usage_from_message(body) if isinstance(body.get("usage"), dict) else None
        stop_reason = body.get("stop_reason")
        return TapResult(usage, None, stop_reason if isinstance(stop_reason, str) else None)


class AnthropicProvider:
    name: str = "anthropic"

    def __init__(self, models: Iterable[str]) -> None:
        self._models = frozenset(models)

    def supports(self, model: str) -> bool:
        return model in self._models

    def send(self, req: AnthropicRequest, model: str) -> AsyncIterator[bytes]:
        raise NotImplementedError("eval sends go through claude -p (T5.2)")

    def parse_usage(self, events: list[dict[str, Any]]) -> Usage:
        return usage_from_events(events)
