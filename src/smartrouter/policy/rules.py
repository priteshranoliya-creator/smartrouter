"""The seven ordered policy rules. First rule to return a decision wins."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Literal

from smartrouter.policy.cost_estimator import cache_ttl, saving_per_turn, switch_tax
from smartrouter.settings import PolicyConfig, TierConfig
from smartrouter.telemetry.pricing import PriceBook

RuleName = Literal["override", "low_confidence", "same_tier", "free_window", "upgrade", "cooldown", "payback"]
Override = Literal["opus", "sonnet", "haiku", "auto"]


@dataclass(frozen=True)
class PolicyInput:
    desired_tier: str
    tier_probability: float
    current_tier: str | None
    override: Override | None
    prefix_tokens: int
    warm_tokens_by_tier: Mapping[str, int]
    seconds_since_last_request: float | None
    just_compacted: bool
    turns_since_switch: int | None
    est_output_tokens: float
    expected_turns: float
    on: date


@dataclass(frozen=True)
class PolicyDecision:
    """`chosen_tier` is None only for a new session the policy declined to decide (low confidence)."""

    chosen_tier: str | None
    desired_tier: str
    rule: RuleName
    switch: bool
    est_switch_tax_usd: float | None
    est_saving_usd: float | None


@dataclass(frozen=True)
class PolicyContext:
    config: PolicyConfig
    tiers: Mapping[str, TierConfig]
    book: PriceBook


Rule = Callable[[PolicyInput, PolicyContext], PolicyDecision | None]


def _estimates(
    inp: PolicyInput, ctx: PolicyContext, considered: str | None
) -> tuple[float | None, float | None]:
    current = inp.current_tier
    if current is None or considered is None or considered == current:
        return None, None
    current_model = ctx.tiers[current].model
    target_model = ctx.tiers[considered].model
    tax = switch_tax(
        ctx.book,
        current_model,
        target_model,
        inp.prefix_tokens,
        inp.warm_tokens_by_tier.get(considered, 0),
        cache_ttl(ctx.config),
        inp.on,
    )
    saving = (
        saving_per_turn(
            ctx.book, current_model, target_model, inp.prefix_tokens, inp.est_output_tokens, inp.on
        )
        * inp.expected_turns
    )
    return tax, saving


def _outcome(
    inp: PolicyInput,
    ctx: PolicyContext,
    rule: RuleName,
    chosen: str | None,
    considered: str | None = None,
) -> PolicyDecision:
    tax, saving = _estimates(inp, ctx, considered)
    return PolicyDecision(
        chosen_tier=chosen,
        desired_tier=inp.desired_tier,
        rule=rule,
        switch=chosen is not None and chosen != inp.current_tier,
        est_switch_tax_usd=tax,
        est_saving_usd=saving,
    )


def override(inp: PolicyInput, ctx: PolicyContext) -> PolicyDecision | None:
    if inp.override is None or inp.override == "auto":
        return None
    return _outcome(inp, ctx, "override", inp.override, considered=inp.override)


def low_confidence(inp: PolicyInput, ctx: PolicyContext) -> PolicyDecision | None:
    if inp.tier_probability >= ctx.config.min_confidence:
        return None
    return _outcome(inp, ctx, "low_confidence", inp.current_tier)


def same_tier(inp: PolicyInput, ctx: PolicyContext) -> PolicyDecision | None:
    if inp.desired_tier != inp.current_tier:
        return None
    return _outcome(inp, ctx, "same_tier", inp.current_tier)


def free_window(inp: PolicyInput, ctx: PolicyContext) -> PolicyDecision | None:
    idle = inp.seconds_since_last_request
    cold = idle is not None and idle > ctx.config.cache_ttl_s
    if not (inp.current_tier is None or inp.just_compacted or cold):
        return None
    return _outcome(inp, ctx, "free_window", inp.desired_tier, considered=inp.desired_tier)


def upgrade(inp: PolicyInput, ctx: PolicyContext) -> PolicyDecision | None:
    current = inp.current_tier
    if current is None:
        return None
    stronger = ctx.tiers[inp.desired_tier].strength > ctx.tiers[current].strength
    if not (stronger and inp.tier_probability >= ctx.config.upgrade_confidence):
        return None
    return _outcome(inp, ctx, "upgrade", inp.desired_tier, considered=inp.desired_tier)


def cooldown(inp: PolicyInput, ctx: PolicyContext) -> PolicyDecision | None:
    since = inp.turns_since_switch
    if since is None or since >= ctx.config.min_turns_between_switches:
        return None
    return _outcome(inp, ctx, "cooldown", inp.current_tier, considered=inp.desired_tier)


def payback(inp: PolicyInput, ctx: PolicyContext) -> PolicyDecision | None:
    """A cheaper tier must repay its switch tax plus margin. A stronger one below the upgrade bar stays."""
    current = inp.current_tier
    if current is None:
        return None
    tax, saving = _estimates(inp, ctx, inp.desired_tier)
    cheaper = ctx.tiers[inp.desired_tier].strength < ctx.tiers[current].strength
    pays = cheaper and tax is not None and saving is not None and saving > tax * (1 + ctx.config.margin)
    chosen = inp.desired_tier if pays else current
    return _outcome(inp, ctx, "payback", chosen, considered=inp.desired_tier)


RULES: tuple[Rule, ...] = (override, low_confidence, same_tier, free_window, upgrade, cooldown, payback)
