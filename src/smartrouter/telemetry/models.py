"""One frozen dataclass per table row. Timestamps are ISO-8601 UTC strings; JSON columns are typed Python."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

SessionState = Literal["active", "compacting", "ended"]
Override = Literal["opus", "sonnet", "haiku", "auto"]
Rule = Literal[
    "override",
    "low_confidence",
    "same_tier",
    "free_window",
    "upgrade",
    "cooldown",
    "payback",
    "decision_timeout",
]
Mode = Literal["off", "shadow", "active"]
RequestKind = Literal["main", "background", "compaction", "subagent", "eval"]
CompactionTrigger = Literal["manual", "auto"]


@dataclass(frozen=True, kw_only=True)
class Session:
    id: str
    project_path: str
    started_at: str
    current_model: str | None = None
    state: SessionState = "active"
    last_request_at: str | None = None
    last_switch_turn: int | None = None


@dataclass(frozen=True, kw_only=True)
class SessionModel:
    """Cache warmth of one model within one session."""

    session_id: str
    model: str
    last_used_at: str
    prefix_tokens: int | None = None


@dataclass(frozen=True, kw_only=True)
class Turn:
    id: int | None = None
    session_id: str
    seq: int
    prompt_id: str | None = None
    prompt: str
    override: Override | None = None
    created_at: str


@dataclass(frozen=True, kw_only=True)
class Decision:
    turn_id: int
    engine: str
    answers: dict[str, Any]
    engine_latency_ms: float | None = None
    desired_model: str | None = None
    chosen_model: str
    rule: Rule
    mode: Mode
    est_switch_tax_usd: float | None = None
    est_saving_usd: float | None = None


@dataclass(frozen=True, kw_only=True)
class Request:
    id: int | None = None
    turn_id: int | None = None
    session_id: str | None = None
    kind: RequestKind
    requested_model: str
    sent_model: str
    provider: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_5m_tokens: int | None = None
    cache_write_1h_tokens: int | None = None
    cost_usd: float
    counterfactual_cost_usd: float | None = None
    latency_ms: float | None = None
    ttft_ms: float | None = None
    status: int | None = None
    error: str | None = None
    created_at: str


@dataclass(frozen=True, kw_only=True)
class Compaction:
    id: int | None = None
    session_id: str
    trigger: CompactionTrigger
    prefix_tokens_before: int | None = None
    created_at: str


@dataclass(frozen=True, kw_only=True)
class EvalRun:
    id: int | None = None
    dataset_version: str
    arms: list[str]
    judge_model: str
    started_at: str
    finished_at: str | None = None


@dataclass(frozen=True, kw_only=True)
class EvalResult:
    id: int | None = None
    run_id: int
    item_id: str
    arm: str
    model_used: str
    answer: str
    judge_score: float | None = None
    rubric_hits: dict[str, Any] | None = None
    cost_usd: float | None = None
    latency_ms: float | None = None
    expected_tier: str | None = None
    predicted_tier: str | None = None
    error: str | None = None


@dataclass(frozen=True, kw_only=True)
class RequestRow:
    """A request with the facts of its turn's decision, when it has one."""

    request: Request
    turn_seq: int | None = None
    mode: Mode | None = None
    rule: Rule | None = None
    chosen_model: str | None = None


@dataclass(frozen=True, kw_only=True)
class DecisionFact:
    """A decision with its turn and the model its turn's first main request asked for."""

    decision: Decision
    turn: Turn
    first_main_requested_model: str | None = None
