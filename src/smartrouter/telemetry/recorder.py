"""Turns one proxied exchange into a requests row, session and turn bookkeeping, and bus events."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime

from smartrouter.decision.service import DecisionService, Outcome
from smartrouter.providers.base import Usage
from smartrouter.telemetry.events import Event, EventBus
from smartrouter.telemetry.models import Decision, Request, RequestKind, Session, SessionModel, Turn
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
    sent_model: str | None = None  # the model upstream ran; None means the request went out as asked
    fell_back: bool = False  # the routed model refused it and it was retried as asked
    overhead_ms: float | None = None


@dataclass(frozen=True)
class Route:
    """Where a main request goes instead of the model it asked for. Shadow mode works it out too, so
    the time it takes is measured, but only active mode applies it."""

    model: str
    turn_id: int
    apply: bool


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


def _sent(ex: Exchange) -> str | None:
    return ex.sent_model or ex.request_model


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
        self.decisions = decisions  # replaced when the runtime mode changes
        # Turns that fell back in this process. The fallback row's error marks them for good, but that
        # row is written after the response, possibly after the turn's next request has arrived.
        self._fell_back: set[int] = set()

    def route(
        self, headers: Mapping[str, str], request_model: str | None, metadata_session_id: str | None
    ) -> Route | None:
        """The model a main request goes out on in active mode; None to send it as asked.

        Never waits for the engine (tdd-changes #28): the turn's decision once it is known, else the
        turn's override, else the model the session runs now. Models outside the tiers pass as asked.
        """
        decisions = self.decisions
        if decisions is None or not decisions.is_tier_model(request_model):
            return None
        resolved = self._resolve_parts(headers, request_model, metadata_session_id)
        turn, session = resolved.turn, resolved.session
        if resolved.kind != "main" or session is None or turn is None or turn.id is None:
            return None
        if turn.id in self._fell_back or self._repo.turn_fell_back(turn.id):
            return None
        existing = self._repo.get_decision(turn.id)
        if existing is not None:
            target: str | None = existing.chosen_model
        else:
            outcome = decisions.ready(turn.id)
            chosen = None if outcome is None else outcome.chosen_model
            override = None if turn.override is None else decisions.tier_model(turn.override)
            target = chosen or override or session.current_model
        if not decisions.is_tier_model(target):
            return None
        if target is None or target == request_model:
            return None
        return Route(target, turn.id, apply=decisions.mode == "active")

    def fell_back(self, turn_id: int) -> None:
        """The routed model failed for this turn: its later requests go out as asked."""
        self._fell_back.add(turn_id)

    async def arecord(self, ex: Exchange) -> int | None:
        """Record the exchange. A turn's first main request waits for the turn's decision, to its deadline."""
        if not _is_messages(ex):
            return None
        resolved = self._resolve(ex)
        turn = resolved.turn
        decisions = self.decisions
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
        decision = self._turn_choice(ex, resolved, outcome)
        # Shadow: what the router's choice would have cost. Active: what the asked model would have.
        alternative = (
            None
            if decision is None
            else (ex.request_model if decision.mode == "active" else decision.chosen_model)
        )
        first_in_turn = (
            decision is not None
            and resolved.turn is not None
            and resolved.turn.id is not None
            and self._repo.main_request_count(resolved.turn.id) == 0
        )
        row = self._build_row(ex, resolved)
        row = replace(
            row, counterfactual_cost_usd=self._counterfactual(ex, resolved, alternative, first_in_turn)
        )
        row = replace(row, id=self._repo.insert_request(row))
        self._update_session(ex, resolved)
        self._bus.publish(Event("request", asdict(row)))
        return row.id

    def _turn_choice(self, ex: Exchange, resolved: _Resolved, outcome: Outcome | None) -> Decision | None:
        """The turn's decision, written on the turn's first main request."""
        turn = resolved.turn
        if self.decisions is None or resolved.kind != "main" or turn is None or turn.id is None:
            return None
        existing = self._repo.get_decision(turn.id)
        if existing is not None:
            return existing
        row = self.decisions.decision_row(turn.id, outcome, _sent(ex) or "unknown")
        if self._repo.insert_decision_if_absent(row):
            self._bus.publish(Event("decision", asdict(row)))
        return row

    def _counterfactual(
        self, ex: Exchange, resolved: _Resolved, chosen: str | None, first_in_turn: bool
    ) -> float | None:
        """Cost had `chosen` served the request. A turn's first request finds that model's prefix warm
        only if the session used it within the TTL; later requests read what the actual request read."""
        usage, model = ex.usage, _sent(ex)
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
        assert self.decisions is not None
        if session is None:
            return 0
        used = self._repo.get_session_model(session.id, model)
        if used is None:
            return 0
        idle = (now - datetime.fromisoformat(used.last_used_at)).total_seconds()
        return (used.prefix_tokens or 0) if idle <= self.decisions.cache_ttl_s else 0

    def _resolve(self, ex: Exchange) -> _Resolved:
        return self._resolve_parts(
            ex.request_headers, ex.request_model, ex.metadata_session_id, ex.started_at
        )

    def _resolve_parts(
        self,
        headers: Mapping[str, str],
        request_model: str | None,
        metadata_session_id: str | None,
        at: datetime | None = None,
    ) -> _Resolved:
        class_header = headers.get(CLASS_HEADER)
        session_id = headers.get(SESSION_HEADER) or metadata_session_id
        if session_id is None:
            return _Resolved(None, None, resolve_kind(class_header, request_model, None))
        started = (at or datetime.now(UTC)).astimezone(UTC).isoformat()
        self._repo.ensure_session(Session(id=session_id, project_path="", started_at=started))
        session = self._repo.get_session(session_id)
        assert session is not None
        prompt_id = headers.get(PROMPT_HEADER)
        turn = choose_turn(
            None if prompt_id is None else self._repo.get_turn_by_prompt_id(session_id, prompt_id),
            self._repo.latest_turn(session_id),
        )
        return _Resolved(session, turn, resolve_kind(class_header, request_model, session.current_model))

    def _build_row(self, ex: Exchange, resolved: _Resolved) -> Request:
        when = ex.started_at.astimezone(UTC)
        model = _sent(ex) or "unknown"
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
            requested_model=ex.request_model or "unknown",
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
            overhead_ms=ex.overhead_ms,
            created_at=when.isoformat(),
        )

    def _update_session(self, ex: Exchange, resolved: _Resolved) -> None:
        session, model = resolved.session, _sent(ex)
        if session is None or resolved.kind != "main" or model is None:
            return
        if not 200 <= ex.status < 300:
            return
        at = ex.started_at.astimezone(UTC).isoformat()
        moved = session.current_model is not None and model != session.current_model and not ex.fell_back
        if (
            moved
            and resolved.turn is not None
            and self.decisions is not None
            and self.decisions.mode == "active"
        ):
            self._repo.set_last_switch_turn(session.id, resolved.turn.seq)  # starts the switch cooldown
        self._repo.mark_session_request(session.id, model, at)
        if ex.usage is not None:
            self._repo.touch_session_model(
                SessionModel(
                    session_id=session.id,
                    model=model,
                    last_used_at=at,
                    prefix_tokens=ex.usage.prompt_tokens,
                )
            )
