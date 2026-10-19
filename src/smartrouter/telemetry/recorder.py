"""Turns one proxied exchange into a requests row and a bus event."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime

from smartrouter.providers.base import Usage
from smartrouter.telemetry.events import Event, EventBus
from smartrouter.telemetry.models import Request, RequestKind
from smartrouter.telemetry.pricing import PriceBook, PricingError, cost
from smartrouter.telemetry.repo import Repo

SESSION_HEADER = "x-claude-code-session-id"
CLASS_HEADER = "x-claude-code-request-class"

KIND_BY_CLASS: dict[str, RequestKind] = {
    "main": "main",
    "subagent": "subagent",
    "workflow": "subagent",
    "compaction": "compaction",
    "auxiliary": "background",
}


@dataclass(frozen=True)
class Exchange:
    started_at: datetime
    method: str
    path: str
    request_headers: Mapping[str, str]
    request_model: str | None
    status: int
    usage: Usage | None
    latency_ms: float
    ttft_ms: float | None
    error: str | None
    metadata_session_id: str | None = None


class Recorder:
    def __init__(self, repo: Repo, prices: PriceBook, bus: EventBus) -> None:
        self._repo = repo
        self._prices = prices
        self._bus = bus

    def record(self, ex: Exchange) -> int | None:
        if ex.method != "POST" or ex.path != "/v1/messages":
            return None
        headers = ex.request_headers
        when = ex.started_at.astimezone(UTC)
        model = ex.request_model or "unknown"
        usage, error = ex.usage, ex.error
        cost_usd = 0.0
        if usage is not None:
            try:
                cost_usd = cost(self._prices, model, usage, when.date())
            except PricingError:
                error = "; ".join(filter(None, [error, f"unpriced model {model}"]))
        row = Request(
            session_id=headers.get(SESSION_HEADER) or ex.metadata_session_id,
            kind=KIND_BY_CLASS.get(headers.get(CLASS_HEADER, ""), "main"),
            requested_model=model,
            sent_model=model,
            provider="anthropic",
            input_tokens=usage.input_tokens if usage else None,
            output_tokens=usage.output_tokens if usage else None,
            cache_read_tokens=usage.cache_read_tokens if usage else None,
            cache_write_5m_tokens=usage.cache_write_5m_tokens if usage else None,
            cache_write_1h_tokens=usage.cache_write_1h_tokens if usage else None,
            cost_usd=cost_usd,
            latency_ms=ex.latency_ms,
            ttft_ms=ex.ttft_ms,
            status=ex.status,
            error=error,
            created_at=when.isoformat(),
        )
        row_id = self._repo.insert_request(row)
        self._bus.publish(Event("request", asdict(replace(row, id=row_id))))
        return row_id
