import gzip
import random
from pathlib import Path

import pytest

from smartrouter.providers.anthropic import (
    AnthropicProvider,
    StreamTap,
    TapResult,
    usage_from_events,
    usage_from_message,
)
from smartrouter.providers.base import Usage

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "anthropic"
SSE = "text/event-stream"

EXPECTED = {
    "text": Usage(
        input_tokens=2,
        output_tokens=4,
        cache_read_tokens=35617,
        cache_write_5m_tokens=0,
        inference_geo="global",
    ),
    "tool_use": Usage(
        input_tokens=2,
        output_tokens=121,
        cache_read_tokens=32411,
        cache_write_1h_tokens=3246,
        inference_geo="global",
    ),
    "text_after_tool": Usage(
        input_tokens=2,
        output_tokens=4,
        cache_read_tokens=35657,
        cache_write_1h_tokens=140,
        inference_geo="global",
    ),
}


def tap_over(
    data: bytes, content_type: str, encoding: str | None, sizes: list[int] | None = None
) -> TapResult:
    tap = StreamTap(content_type, encoding)
    pos = 0
    for size in sizes or [len(data) or 1]:
        tap.feed(data[pos : pos + size])
        pos += size
    tap.feed(data[pos:])
    return tap.finish()


def sizes_for(label: str, total: int) -> list[int]:
    if label == "1":
        return [1] * total
    if label == "7":
        return [7] * (total // 7 + 1)
    rng = random.Random(1234)
    out: list[int] = []
    while sum(out) < total:
        out.append(rng.randint(1, 200))
    return out


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_usage_from_each_recorded_stream(name: str) -> None:
    result = tap_over((FIXTURES / f"{name}.sse").read_bytes(), SSE, None)
    assert result.usage == EXPECTED[name]
    assert result.error is None


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_gzip_and_identity_give_the_same_usage(name: str) -> None:
    identity = tap_over((FIXTURES / f"{name}.sse").read_bytes(), SSE, None)
    zipped = tap_over((FIXTURES / f"{name}.sse.gz").read_bytes(), SSE, "gzip")
    assert zipped == identity


@pytest.mark.parametrize("chunking", ["1", "7", "random"])
@pytest.mark.parametrize("name", sorted(EXPECTED))
@pytest.mark.parametrize("encoding", [None, "gzip"])
def test_chunk_boundaries_do_not_change_the_result(name: str, encoding: str | None, chunking: str) -> None:
    path = FIXTURES / (f"{name}.sse.gz" if encoding else f"{name}.sse")
    data = path.read_bytes()
    result = tap_over(data, SSE, encoding, sizes_for(chunking, len(data)))
    assert result.usage == EXPECTED[name]


def test_stop_reason_comes_from_message_delta() -> None:
    assert tap_over((FIXTURES / "tool_use.sse").read_bytes(), SSE, None).stop_reason == "tool_use"
    assert tap_over((FIXTURES / "text.sse").read_bytes(), SSE, None).stop_reason == "end_turn"


def test_deflate_is_decoded() -> None:
    import zlib

    raw = (FIXTURES / "text.sse").read_bytes()
    assert tap_over(zlib.compress(raw), SSE, "deflate").usage == EXPECTED["text"]


def test_crlf_streams_parse() -> None:
    raw = (FIXTURES / "text.sse").read_bytes().replace(b"\n", b"\r\n")
    result = tap_over(raw, SSE, None, sizes_for("1", len(raw)))
    assert result.usage == EXPECTED["text"]


def test_final_event_without_blank_line_still_counts() -> None:
    raw = (FIXTURES / "text.sse").read_bytes().rstrip(b"\n")
    assert tap_over(raw, SSE, None).usage == EXPECTED["text"]


def test_non_streaming_body_has_the_5m_1h_split() -> None:
    expected = Usage(
        input_tokens=12,
        output_tokens=5,
        cache_read_tokens=30000,
        cache_write_5m_tokens=1000,
        cache_write_1h_tokens=2000,
        inference_geo="global",
    )
    data = (FIXTURES / "non_streaming.json").read_bytes()
    assert tap_over(data, "application/json", None, sizes_for("7", len(data))).usage == expected
    assert tap_over(gzip.compress(data), "application/json", "gzip").usage == expected
    import json

    assert usage_from_message(json.loads(data)) == expected


def test_error_body_gives_the_message_and_no_usage() -> None:
    result = tap_over((FIXTURES / "error_400.json").read_bytes(), "application/json", None)
    assert result.error == "model: claude-nope is not a valid model"
    assert result.usage is None


def test_sse_error_event_gives_the_message() -> None:
    raw = (
        b'event: error\ndata: {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}\n\n'
    )
    result = tap_over(raw, SSE, None)
    assert result.error == "Overloaded"
    assert result.usage is None


def test_no_split_anywhere_counts_all_cache_writes_as_5m() -> None:
    events = [
        {
            "type": "message_start",
            "message": {"usage": {"input_tokens": 1, "cache_creation_input_tokens": 90}},
        },
        {"type": "message_delta", "usage": {"output_tokens": 7, "cache_creation_input_tokens": None}},
    ]
    assert usage_from_events(events) == Usage(input_tokens=1, output_tokens=7, cache_write_5m_tokens=90)


def test_split_from_message_start_survives_a_delta_without_one() -> None:
    events = [
        {
            "type": "message_start",
            "message": {
                "usage": {
                    "cache_creation_input_tokens": 30,
                    "cache_creation": {"ephemeral_5m_input_tokens": 10, "ephemeral_1h_input_tokens": 20},
                }
            },
        },
        {"type": "message_delta", "usage": {"cache_creation_input_tokens": 30, "output_tokens": 3}},
    ]
    usage = usage_from_events(events)
    assert (usage.cache_write_5m_tokens, usage.cache_write_1h_tokens, usage.output_tokens) == (10, 20, 3)


@pytest.mark.parametrize("encoding", [None, "gzip", "deflate", "br", "zstd"])
@pytest.mark.parametrize("content_type", [SSE, "application/json", "text/plain", ""])
def test_garbage_never_raises(content_type: str, encoding: str | None) -> None:
    rng = random.Random(7)
    garbage = bytes(rng.randrange(256) for _ in range(500))
    result = tap_over(garbage, content_type, encoding, sizes_for("7", len(garbage)))
    assert result.usage is None


def test_unsupported_encoding_disables_parsing_without_failing_the_relay() -> None:
    data = (FIXTURES / "text.sse").read_bytes()
    result = tap_over(data, SSE, "br")
    assert result.usage is None
    assert result.error is not None


def test_provider_supports_only_its_configured_models() -> None:
    provider = AnthropicProvider(["model-a"])
    assert provider.name == "anthropic"
    assert provider.supports("model-a")
    assert not provider.supports("model-b")
    assert provider.parse_usage([]) == Usage()
    with pytest.raises(NotImplementedError, match=r"claude -p"):
        provider.send({}, "model-a")
