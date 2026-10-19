"""Prompt features for the decision engine: the `@model` override prefix and the engine's input state."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from smartrouter.decision.engine import DecisionState
from smartrouter.telemetry.models import Override

_OVERRIDES: dict[str, Override] = {"opus": "opus", "sonnet": "sonnet", "haiku": "haiku", "auto": "auto"}
_OVERRIDE_PREFIX = re.compile(rf"\s*@({'|'.join(_OVERRIDES)})(?=\s|$)", re.IGNORECASE)

HEAD_TAIL_CHARS = 1200
MIN_KEEP_CHARS = 200
_ELISION = "\n...\n"
_PREVIOUS_PROMPT_CHARS = 160


def parse_override(prompt: str) -> Override | None:
    """The model the user pinned with a leading `@opus`, `@sonnet`, `@haiku` or `@auto`, else None."""
    match = _OVERRIDE_PREFIX.match(prompt)
    return None if match is None else _OVERRIDES[match.group(1).lower()]


@dataclass(frozen=True)
class SessionView:
    turn_seq: int
    current_tier: str | None
    minutes_since_last_request: float | None
    just_compacted: bool
    previous_prompt: str | None


def estimate_tokens(text: str) -> int:
    # Conservative upper bound without a tokenizer: UTF-8 bytes / 3 overestimates English (~4 chars per
    # token) and tracks CJK, Devanagari and emoji, which tokenizers split into several tokens.
    return math.ceil(len(text.encode("utf-8")) / 3)


def _strip_override(prompt: str) -> str:
    match = _OVERRIDE_PREFIX.match(prompt)
    return prompt if match is None else prompt[match.end() :].lstrip()


def _clip(prompt: str, keep: int) -> str:
    if len(prompt) <= 2 * keep:
        return prompt
    return prompt[:keep] + _ELISION + prompt[len(prompt) - keep :]


def _facts(session: SessionView | None) -> list[str]:
    if session is None:
        return []
    facts = [f"turn {session.turn_seq} of this session"]
    if session.current_tier is not None:
        facts.append(f"current model tier: {session.current_tier}")
    if session.minutes_since_last_request is not None and round(session.minutes_since_last_request) >= 1:
        facts.append(f"idle {round(session.minutes_since_last_request)} minutes")
    if session.just_compacted:
        facts.append("conversation was just compacted")
    if session.previous_prompt:
        previous = " ".join(session.previous_prompt.split())[:_PREVIOUS_PROMPT_CHARS]
        if previous:
            facts.append(f"previous request: {previous}")
    return facts


def build_state(prompt: str, session: SessionView | None, budget_tokens: int = 320) -> DecisionState:
    """The engine's view: the override-free prompt, head and tail kept, plus short session facts.

    Over budget, the prompt's head and tail shrink first, down to MIN_KEEP_CHARS each; only then are
    facts dropped from the end, since they are short and carry the session context.
    """
    text = _strip_override(prompt)
    facts = _facts(session)

    def state(keep: int, fact_count: int) -> DecisionState:
        return DecisionState(prompt=_clip(text, keep), facts=tuple(facts[:fact_count]))

    def fits(candidate: DecisionState) -> bool:
        return estimate_tokens(candidate.render()) <= budget_tokens

    for fact_count in range(len(facts), -1, -1):
        floor = MIN_KEEP_CHARS if fact_count else 0
        if not fits(state(floor, fact_count)):
            continue
        low, high = floor, HEAD_TAIL_CHARS  # estimate grows with keep: find the largest keep that fits
        while low < high:
            mid = (low + high + 1) // 2
            if fits(state(mid, fact_count)):
                low = mid
            else:
                high = mid - 1
        return state(low, fact_count)
    return state(0, 0)
