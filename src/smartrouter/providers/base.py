"""Provider-neutral types shared by the proxy, the providers and pricing."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Usage:
    """Token counts for one request, in Anthropic's terms. Gemini thinking tokens count as output."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    inference_geo: str | None = None

    @property
    def cache_write_tokens(self) -> int:
        return self.cache_write_5m_tokens + self.cache_write_1h_tokens

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens


AnthropicRequest = dict[str, Any]


class Provider(Protocol):
    name: str

    def supports(self, model: str) -> bool: ...

    def send(self, req: AnthropicRequest, model: str) -> AsyncIterator[bytes]: ...

    def parse_usage(self, events: list[dict[str, Any]]) -> Usage: ...
