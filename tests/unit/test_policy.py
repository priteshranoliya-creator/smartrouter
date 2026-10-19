from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import get_args

import pytest

from smartrouter.policy.engine import decide_policy, expected_turns
from smartrouter.policy.rules import RULES, PolicyContext, PolicyDecision, PolicyInput, RuleName
from smartrouter.settings import PolicyConfig, TierConfig
from smartrouter.telemetry.pricing import load_pricing

ROOT = Path(__file__).resolve().parents[2]
DAY = date(2026, 10, 19)

CFG = PolicyConfig(
    min_confidence=0.6,
    upgrade_confidence=0.75,
    min_turns_between_switches=3,
    margin=0.2,
    cache_ttl_s=3600,
    expected_turns_default=8,
    expected_turns_min_sessions=20,
)
TIERS = {
    "haiku": TierConfig(model="claude-haiku-5-5", provider="anthropic", strength=1),
    "sonnet": TierConfig(model="claude-sonnet-5-5", provider="anthropic", strength=2),
    "opus": TierConfig(model="claude-opus-5-5", provider="anthropic", strength=3),
}
CTX = PolicyContext(config=CFG, tiers=TIERS, book=load_pricing(ROOT / "config/pricing.yaml"))

# Base input: on Opus with a 400k prefix, the engine wants Sonnet, no override, hot cache.
# The 1h TTL applies (cache_ttl_s = 3600), so the cold switch tax is
#   Sonnet writes 400k at 4.00/M = 1.60, minus Opus reads 400k at 0.20/M = 0.08  ->  1.52
# and the saving is 400k x (0.20 - 0.10) + 2000 x (20 - 10) = 0.04 + 0.02 = 0.06 per turn.
# With 8 expected turns: saving 0.48 against a bar of 1.52 x 1.2 = 1.824, so it stays.
BASE = PolicyInput(
    desired_tier="sonnet",
    tier_probability=0.9,
    current_tier="opus",
    override=None,
    prefix_tokens=400_000,
    warm_tokens_by_tier={},
    seconds_since_last_request=60.0,
    just_compacted=False,
    turns_since_switch=10,
    est_output_tokens=2000,
    expected_turns=8,
    on=DAY,
)
TAX_OPUS_TO_SONNET = 1.52
SAVING_8_TURNS = 0.48


def _input(**changes: object) -> PolicyInput:
    return replace(BASE, **changes)  # type: ignore[arg-type]


def test_rule_registry_matches_documented_order() -> None:
    assert [rule.__name__ for rule in RULES] == list(get_args(RuleName))
    assert [rule.__name__ for rule in RULES] == [
        "override",
        "low_confidence",
        "same_tier",
        "free_window",
        "upgrade",
        "cooldown",
        "payback",
    ]


# (changes to BASE, rule, chosen_tier, switch, est_switch_tax_usd, est_saving_usd)
# Each row also shows the earlier rules did not fire: test_earlier_rules_stay_silent.
RULE_CASES = [
    pytest.param(
        # override beats low confidence. Opus -> Haiku at 400k, 1h: Haiku (upper tier) writes at 1.00/M
        # = 0.40, minus Opus read 0.08 = 0.32. Saving 0.095 x 8 = 0.76
        {"override": "haiku", "tier_probability": 0.1},
        "override",
        "haiku",
        True,
        0.32,
        0.76,
        id="override-beats-low-confidence",
    ),
    pytest.param(
        {"override": "opus"}, "override", "opus", False, None, None, id="override-to-current-tier-stays"
    ),
    pytest.param(
        {"override": "sonnet", "desired_tier": "sonnet", "turns_since_switch": 0},
        "override",
        "sonnet",
        True,
        TAX_OPUS_TO_SONNET,
        SAVING_8_TURNS,
        id="override-ignores-cooldown-and-payback",
    ),
    pytest.param(
        {"override": "auto"},
        "payback",
        "opus",
        False,
        TAX_OPUS_TO_SONNET,
        SAVING_8_TURNS,
        id="auto-falls-through",
    ),
    pytest.param(
        {"tier_probability": 0.59, "just_compacted": True, "seconds_since_last_request": 9999.0},
        "low_confidence",
        "opus",
        False,
        None,
        None,
        id="low-confidence-beats-free-window",
    ),
    pytest.param(
        {"tier_probability": 0.5, "current_tier": None},
        "low_confidence",
        None,
        False,
        None,
        None,
        id="low-confidence-new-session-has-no-tier",
    ),
    pytest.param(
        {"desired_tier": "opus", "just_compacted": True, "seconds_since_last_request": 9999.0},
        "same_tier",
        "opus",
        False,
        None,
        None,
        id="same-tier-beats-free-window",
    ),
    pytest.param(
        {"current_tier": None, "turns_since_switch": None},
        "free_window",
        "sonnet",
        True,
        None,
        None,
        id="free-window-new-session",
    ),
    pytest.param(
        # compaction rewrites the cache, so the tax is moot, but the estimate is still reported
        {"just_compacted": True, "turns_since_switch": 0},
        "free_window",
        "sonnet",
        True,
        TAX_OPUS_TO_SONNET,
        SAVING_8_TURNS,
        id="free-window-compacted-beats-cooldown",
    ),
    pytest.param(
        {"seconds_since_last_request": 3601.0, "turns_since_switch": 0},
        "free_window",
        "sonnet",
        True,
        TAX_OPUS_TO_SONNET,
        SAVING_8_TURNS,
        id="free-window-cold-beats-cooldown",
    ),
    pytest.param(
        # Sonnet -> Opus at 400k, 1h: Opus writes at 8.00/M = 3.20, minus Sonnet read 0.04 = 3.16.
        # Saving 400k x (0.10 - 0.20) + 2000 x (10 - 20) = -0.06 per turn, x 8 = -0.48
        {"current_tier": "sonnet", "desired_tier": "opus", "tier_probability": 0.75, "turns_since_switch": 0},
        "upgrade",
        "opus",
        True,
        3.16,
        -0.48,
        id="upgrade-beats-cooldown",
    ),
    pytest.param(
        # cooldown stays even though 100 turns x 0.06 = 6.00 would clear 1.824
        {"turns_since_switch": 2, "expected_turns": 100},
        "cooldown",
        "opus",
        False,
        TAX_OPUS_TO_SONNET,
        6.0,
        id="cooldown-blocks-a-paying-downgrade",
    ),
    pytest.param(
        # 30.5 turns x 0.06 = 1.83 > 1.824
        {"expected_turns": 30.5},
        "payback",
        "sonnet",
        True,
        TAX_OPUS_TO_SONNET,
        1.83,
        id="payback-switches",
    ),
    pytest.param(
        {},
        "payback",
        "opus",
        False,
        TAX_OPUS_TO_SONNET,
        SAVING_8_TURNS,
        id="payback-stays-when-it-does-not-pay",
    ),
    pytest.param(
        # stronger tier below the 0.75 upgrade bar: stay with rule payback (tdd-changes #8)
        {"current_tier": "sonnet", "desired_tier": "opus", "tier_probability": 0.7},
        "payback",
        "sonnet",
        False,
        3.16,
        -0.48,
        id="fall-through-stronger-below-upgrade-bar",
    ),
]


@pytest.mark.parametrize(("changes", "rule", "chosen", "switch", "tax", "saving"), RULE_CASES)
def test_decide_policy(
    changes: dict[str, object],
    rule: RuleName,
    chosen: str | None,
    switch: bool,
    tax: float | None,
    saving: float | None,
) -> None:
    inp = _input(**changes)
    decision = decide_policy(inp, CTX)
    assert decision.rule == rule
    assert decision.chosen_tier == chosen
    assert decision.switch is switch
    assert decision.desired_tier == inp.desired_tier
    assert decision.est_switch_tax_usd == pytest.approx(tax)
    assert decision.est_saving_usd == pytest.approx(saving)


@pytest.mark.parametrize(
    ("changes", "rule"),
    [pytest.param(p.values[0], p.values[1], id=p.id) for p in RULE_CASES],
)
def test_earlier_rules_stay_silent(changes: dict[str, object], rule: RuleName) -> None:
    inp = _input(**changes)
    names = [r.__name__ for r in RULES]
    position = names.index(rule)
    assert [r.__name__ for r in RULES[:position] if r(inp, CTX) is not None] == []
    winner = RULES[position](inp, CTX)
    assert winner is not None
    assert winner == decide_policy(inp, CTX)


def test_every_rule_is_exercised_by_a_case() -> None:
    fired = {decide_policy(_input(**p.values[0]), CTX).rule for p in RULE_CASES}
    assert fired == set(get_args(RuleName))


# (changes to BASE, rule just inside the boundary vs the rule just outside it)
BOUNDARIES = [
    # min_confidence 0.6: exactly 0.6 acts, just below does not
    pytest.param({"tier_probability": 0.6}, "payback", id="min-confidence-exact-passes"),
    pytest.param({"tier_probability": 0.5999}, "low_confidence", id="min-confidence-just-below"),
    # upgrade_confidence 0.75, Sonnet -> Opus
    pytest.param(
        {"current_tier": "sonnet", "desired_tier": "opus", "tier_probability": 0.75},
        "upgrade",
        id="upgrade-confidence-exact-upgrades",
    ),
    pytest.param(
        {"current_tier": "sonnet", "desired_tier": "opus", "tier_probability": 0.7499},
        "payback",
        id="upgrade-confidence-just-below",
    ),
    # cache_ttl_s 3600: idle exactly the TTL is still warm
    pytest.param({"seconds_since_last_request": 3600.0}, "payback", id="idle-exactly-ttl-is-warm"),
    pytest.param({"seconds_since_last_request": 3601.0}, "free_window", id="idle-ttl-plus-one-is-cold"),
    pytest.param({"seconds_since_last_request": None}, "payback", id="no-previous-request-is-not-cold"),
    # min_turns_between_switches 3
    pytest.param({"turns_since_switch": 3, "expected_turns": 100}, "payback", id="cooldown-exact-allowed"),
    pytest.param({"turns_since_switch": 2, "expected_turns": 100}, "cooldown", id="cooldown-one-below"),
    pytest.param({"turns_since_switch": 0, "expected_turns": 100}, "cooldown", id="cooldown-just-switched"),
    pytest.param(
        {"turns_since_switch": None, "expected_turns": 100}, "payback", id="cooldown-never-switched"
    ),
]


@pytest.mark.parametrize(("changes", "rule"), BOUNDARIES)
def test_boundaries(changes: dict[str, object], rule: RuleName) -> None:
    assert decide_policy(_input(**changes), CTX).rule == rule


# Payback bar is tax x (1 + margin) = 1.52 x 1.2 = 1.824; saving is 0.06 per turn.
# 30.3 turns -> 1.818 (below), 30.5 turns -> 1.83 (above).
@pytest.mark.parametrize(
    ("turns", "switch"),
    [(30.3, False), (30.5, True)],
    ids=["just-below-margin", "just-above-margin"],
)
def test_payback_margin_threshold(turns: float, switch: bool) -> None:
    decision = decide_policy(_input(expected_turns=turns), CTX)
    assert decision.rule == "payback"
    assert decision.switch is switch


def test_payback_without_margin_would_switch_inside_the_margin_band() -> None:
    no_margin = PolicyContext(config=CFG.model_copy(update={"margin": 0.0}), tiers=TIERS, book=CTX.book)
    # 30.3 turns x 0.06 = 1.818 > 1.52, which a 0.2 margin rejected above
    assert decide_policy(_input(expected_turns=30.3), no_margin).switch is True


# Per-model warmth (tdd-changes #12). Opus -> Sonnet, 1h TTL, 400k prefix.
WARMTH = [
    # cold at 1 expected turn: tax 1.52, saving 0.06 -> stays
    pytest.param({}, 1, False, TAX_OPUS_TO_SONNET, id="cold-does-not-pay"),
    # fully warm Sonnet: reads 400k at 0.10 = 0.04, minus Opus read 0.08 = -0.04 tax; saving 0.06 > -0.048
    pytest.param({"sonnet": 400_000}, 1, True, -0.04, id="fully-warm-passes"),
    # 100k warm: 0.01 + 300k x 4.00/M = 1.21, minus 0.08 = 1.13. At 8 turns 0.48 < 1.13 x 1.2 = 1.356
    pytest.param({"sonnet": 100_000}, 8, False, 1.13, id="partly-warm-still-pays-tax"),
    # warmth on another tier does not help the target
    pytest.param({"haiku": 400_000}, 1, False, TAX_OPUS_TO_SONNET, id="other-tier-warmth-ignored"),
]


@pytest.mark.parametrize(("warm", "turns", "switch", "tax"), WARMTH)
def test_per_model_warmth(warm: dict[str, int], turns: float, switch: bool, tax: float) -> None:
    decision = decide_policy(_input(warm_tokens_by_tier=warm, expected_turns=turns), CTX)
    assert decision.rule == "payback"
    assert decision.switch is switch
    assert decision.est_switch_tax_usd == pytest.approx(tax)


def test_small_prefix_haiku_downgrade_has_zero_tax() -> None:
    # Opus -> Haiku at 50k (lower Haiku tier), 1h: Haiku writes at 0.20/M = 0.01, Opus reads 0.01: tax 0.
    # Saving 50k x (0.20 - 0.01) + 2000 x (20 - 0.5) = 0.0095 + 0.039 = 0.0485 per turn, x 8 = 0.388
    decision = decide_policy(_input(desired_tier="haiku", prefix_tokens=50_000), CTX)
    assert decision.rule == "payback"
    assert decision.switch is True
    assert decision.est_switch_tax_usd == pytest.approx(0.0)
    assert decision.est_saving_usd == pytest.approx(0.388)


def test_five_minute_ttl_prices_the_tdd_fixture() -> None:
    # cache_ttl_s 300 selects the 5m write price: Sonnet 2.50/M x 400k = 1.00, minus 0.08 = 0.92
    ctx = PolicyContext(config=CFG.model_copy(update={"cache_ttl_s": 300}), tiers=TIERS, book=CTX.book)
    decision = decide_policy(_input(), ctx)
    assert decision.est_switch_tax_usd == pytest.approx(0.92)


def test_decision_is_a_frozen_value() -> None:
    decision = decide_policy(_input(), CTX)
    assert isinstance(decision, PolicyDecision)
    with pytest.raises(AttributeError):
        decision.switch = True  # type: ignore[misc]


@pytest.mark.parametrize(
    ("median", "sessions", "expected"),
    [
        (None, 100, 8.0),
        (30.0, 0, 8.0),
        (30.0, 19, 8.0),
        (30.0, 20, 30.0),
        (30.0, 21, 30.0),
        (None, 20, 8.0),
    ],
    ids=[
        "no-median",
        "no-sessions",
        "one-below-min-sessions",
        "exactly-min-sessions",
        "above-min-sessions",
        "min-sessions-without-median",
    ],
)
def test_expected_turns(median: float | None, sessions: int, expected: float) -> None:
    assert expected_turns(median, sessions, CFG) == expected


def test_expected_turns_with_zero_min_sessions_trusts_the_median() -> None:
    assert expected_turns(12.5, 0, CFG.model_copy(update={"expected_turns_min_sessions": 0})) == 12.5
