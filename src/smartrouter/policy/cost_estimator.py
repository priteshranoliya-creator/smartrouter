"""Pure cost estimates behind the policy: tier probabilities, switch tax, per-turn saving."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

from smartrouter.decision.calibration import sum_by_tier
from smartrouter.decision.engine import QuestionAnswer
from smartrouter.settings import PolicyConfig
from smartrouter.telemetry.pricing import PriceBook, Ttl, rates

ONE_HOUR_S = 3600


def tier_probabilities(answer: QuestionAnswer, tier_map: Mapping[str, str]) -> dict[str, float]:
    """Sum label probabilities per tier. Every label must be in `tier_map` (settings enforce it)."""
    return sum_by_tier(answer.probabilities, tier_map)


def cache_ttl(cfg: PolicyConfig) -> Ttl:
    return "1h" if cfg.cache_ttl_s >= ONE_HOUR_S else "5m"


def switch_tax(
    book: PriceBook,
    current_model: str,
    target_model: str,
    prefix_tokens: int,
    target_warm_tokens: int,
    ttl: Ttl,
    on: date,
) -> float:
    """Cost of sending the prefix to the target minus reading it on the current model.

    The target reads its warm part and writes the rest for `ttl`. Negative when the target is
    cheaper even to read.
    """
    warm = min(max(target_warm_tokens, 0), prefix_tokens)
    target = rates(book, target_model, prefix_tokens, on)
    current = rates(book, current_model, prefix_tokens, on)
    write = target.cache_write_1h if ttl == "1h" else target.cache_write_5m
    return warm * target.cache_read + (prefix_tokens - warm) * write - prefix_tokens * current.cache_read


def saving_per_turn(
    book: PriceBook,
    current_model: str,
    target_model: str,
    prefix_tokens: int,
    est_output_tokens: float,
    on: date,
) -> float:
    """Per-turn cost avoided by serving from the target: prefix reads plus output."""
    target = rates(book, target_model, prefix_tokens, on)
    current = rates(book, current_model, prefix_tokens, on)
    return prefix_tokens * (current.cache_read - target.cache_read) + est_output_tokens * (
        current.output - target.output
    )
