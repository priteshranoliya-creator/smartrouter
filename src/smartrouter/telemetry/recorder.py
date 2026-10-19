"""Turns one proxied exchange into a requests row, session and turn bookkeeping, and bus events."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime

from smartrouter.decision.service import DecisionService, Outcome
from smartrouter.providers.base import Usage
from smartrouter.telemetry.events import Event, EventBus
from smartrouter.telemetry.models import Request, RequestKind, Session, SessionModel, Turn
from smartrouter.telemetry.pricing import PriceBook, PricingError, cost, counterfactual
from smartrouter.telemetry.repo import Repo

SESSION_HEADER = "x-claude-code-session-id"
PROMPT_HEADER = "x-claude-code-prompt-id"
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


def resolve_kind(
    class_header: str | None, request_model: str | None, current_model: str | None
) -> RequestKind:
    """The class header decides. Without it, main unless the session already runs another model."""
    if class_header is not None:
        return KIND_BY_CLASS.get(class_header, "main")
    if current_model is None or request_model == current_model:
        return "main"
    return "background"


def choose_turn(by_prompt_id: Turn | None, latest: Turn | None) -> Turn | None:
    """The turn named by the prompt-id header, else the session's latest turn."""
    return by_prompt_id or latest


def _is_messages(ex: Exchange) -> bool:
    return ex.method == "POST" and ex.path == "/v1/messages"


@dataclass(frozen=True)
class _Resolved:
    session: Session | None
    turn: Turn | None
    kind: RequestKind


class Recorder:
    def __init__(
        self, repo: Repo, prices: PriceBook, bus: EventBus, decisions: DecisionService | None = None
    ) -> None:
        self._repo = repo
        self._prices = prices
        self._bus = bus
        self._decisions = decisions

    async def arecord(self, ex: Exchange) -> int | None:
        """Record the exchange. A turn's first main request waits for the turn's decision, to its deadline."""
        if not _is_messages(ex):
            return None
        resolved = self._resolve(ex)
        turn = resolved.turn
        decisions = self._decisions
        if (
            decisions is None
            or resolved.kind != "main"
            or turn is None
            or turn.id is None
            or self._repo.get_decision(turn.id) is not None
        ):
            return self._persist(ex, resolved, None)
        try:
            return self._persist(ex, resolved, await decisions.outcome(turn.id))
        finally:
            decisions.release(turn.id)

    def record(self, ex: Exchange, outcome: Outcome | None = None) -> int | None:
        if not _is_messages(ex):
            return None
        return self._persist(ex, self._resolve(ex), outcome)

    def _persist(self, ex: Exchange, resolved: _Resolved, outcome: Outcome | None) -> int | None:
        chosen = self._turn_choice(ex, resolved, outcome)
        first_in_turn = (
            chosen is not None
            and resolved.turn is not None
            and resolved.turn.id is not None
            and self._repo.main_request_count(resolved.turn.id) == 0
        )
        row = self._build_row(ex, resolved)
        row = replace(row, counterfactual_cost_usd=self._counterfactual(ex, resolved, chosen, first_in_turn))
        row = replace(row, id=self._repo.insert_request(row))
        self._update_session(ex, resolved)
        self._bus.publish(Event("request", asdict(row)))
        return row.id

    def _turn_choice(self, ex: Exchange, resolved: _Resolved, outcome: Outcome | None) -> str | None:
        """The model the turn's decision chose, writing the decision on the turn's first main request."""
        turn = resolved.turn
        if self._decisions is None or resolved.kind != "main" or turn is None or turn.id is None:
            return None
        existing = self._repo.get_decision(turn.id)
        if existing is not None:
            return existing.chosen_model
        row = self._decisions.decision_row(turn.id, outcome, ex.request_model or "unknown")
        if self._repo.insert_decision_if_absent(row):
            self._bus.publish(Event("decision", asdict(row)))
        return row.chosen_model

    def _counterfactual(
        self, ex: Exchange, resolved: _Resolved, chosen: str | None, first_in_turn: bool
    ) -> float | None:
        """Cost had `chosen` served the request. A turn's first request finds that model's prefix warm
        only if the session used it within the TTL; later requests read what the actual request read."""
        usage, model = ex.usage, ex.request_model
        if chosen is None or usage is None or model is None:
            return None
        when = ex.started_at.astimezone(UTC)
        try:
            if chosen == model:
                return cost(self._prices, model, usage, when.date())
            if first_in_turn:
                warm = self._warm_prefix(resolved.session, chosen, when)
            else:
                warm = usage.cache_read_tokens
            return counterfactual(self._prices, chosen, usage, warm, when.date())
        except PricingError:
            return None

    def _warm_prefix(self, session: Session | None, model: str, now: datetime) -> int:
        assert self._decisions is not None
        if session is None:
            return 0
        used = self._repo.get_session_model(session.id, model)
        if used is None:
            return 0
        idle = (now - datetime.fromisoformat(used.last_used_at)).total_seconds()
        return (used.prefix_tokens or 0) if idle <= self._decisions.cache_ttl_s else 0

    def _resolve(self, ex: Exchange) -> _Resolved:
        headers = ex.request_headers
        class_header = headers.get(CLASS_HEADER)
        session_id = headers.get(SESSION_HEADER) or ex.metadata_session_id
        if session_id is None:
            return _Resolved(None, None, resolve_kind(class_header, ex.request_model, None))
        self._repo.ensure_session(
            Session(id=session_id, project_path="", started_at=ex.started_at.astimezone(UTC).isoformat())
        )
        session = self._repo.get_session(session_id)
        assert session is not None
        prompt_id = headers.get(PROMPT_HEADER)
        turn = choose_turn(
            None if prompt_id is None else self._repo.get_turn_by_prompt_id(session_id, prompt_id),
            self._repo.latest_turn(session_id),
        )
        return _Resolved(session, turn, resolve_kind(class_header, ex.request_model, session.current_model))

    def _build_row(self, ex: Exchange, resolved: _Resolved) -> Request:
        when = ex.started_at.astimezone(UTC)
        model = ex.request_model or "unknown"
        usage, error = ex.usage, ex.error
        cost_usd = 0.0
        if usage is not None:
            try:
                cost_usd = cost(self._prices, model, usage, when.date())
            except PricingError:
                error = "; ".join(filter(None, [error, f"unpriced model {model}"]))
        return Request(
            turn_id=None if resolved.turn is None else resolved.turn.id,
            session_id=None if resolved.session is None else resolved.session.id,
            kind=resolved.kind,
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

    def _update_session(self, ex: Exchange, resolved: _Resolved) -> None:
        session = resolved.session
        if session is None or resolved.kind != "main" or ex.request_model is None:
            return
        if not 200 <= ex.status < 300:
            return
        at = ex.started_at.astimezone(UTC).isoformat()
        self._repo.mark_session_request(session.id, ex.request_model, at)
        if ex.usage is not None:
            self._repo.touch_session_model(
                SessionModel(
                    session_id=session.id,
                    model=ex.request_model,
                    last_used_at=at,
                    prefix_tokens=ex.usage.prompt_tokens,
                )
            )
