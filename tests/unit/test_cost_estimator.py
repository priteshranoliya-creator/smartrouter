from datetime import date
from pathlib import Path

import pytest

from smartrouter.decision.engine import QuestionAnswer, answer_from_probabilities
from smartrouter.policy.cost_estimator import cache_ttl, saving_per_turn, switch_tax, tier_probabilities
from smartrouter.settings import PolicyConfig
from smartrouter.telemetry.pricing import PriceBook, Ttl, load_pricing

ROOT = Path(__file__).resolve().parents[2]
DAY = date(2026, 10, 19)
OPUS, SONNET, HAIKU = "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"
TIER_MAP = {"A": "haiku", "B": "sonnet", "C": "sonnet", "D": "opus"}


@pytest.fixture(scope="module")
def book() -> PriceBook:
    return load_pricing(ROOT / "config/pricing.yaml")


def _answer(**probabilities: float) -> QuestionAnswer:
    return answer_from_probabilities(probabilities)


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        # B and C both map to sonnet, so 0.3 + 0.4
        (_answer(A=0.1, B=0.3, C=0.4, D=0.2), {"haiku": 0.1, "sonnet": 0.7, "opus": 0.2}),
        (_answer(A=0.0, B=0.5, C=0.5, D=0.0), {"haiku": 0.0, "sonnet": 1.0, "opus": 0.0}),
        (_answer(A=1.0), {"haiku": 1.0}),
    ],
    ids=["b-plus-c", "all-sonnet", "only-listed-labels"],
)
def test_tier_probabilities(answer: QuestionAnswer, expected: dict[str, float]) -> None:
    assert tier_probabilities(answer, TIER_MAP) == pytest.approx(expected)


def test_tier_probabilities_rejects_unmapped_label() -> None:
    with pytest.raises(KeyError):
        tier_probabilities(_answer(Z=1.0), TIER_MAP)


# Prices per 1M tokens from config/pricing.yaml. Cache write = input x 1.25 (5m) or x 2.0 (1h).
#   opus   in 4.00  read 0.20  out 20.00   write 5m 5.00   1h 8.00
#   sonnet in 2.00  read 0.10  out 10.00   write 5m 2.50   1h 4.00
#   haiku  <=100k prompt: in 0.10 read 0.01 out 0.50 write 5m 0.125 1h 0.20
#          > 100k prompt: in 0.50 read 0.05 out 2.50 write 5m 0.625 1h 1.00
@pytest.mark.parametrize(
    ("current", "target", "prefix", "warm", "ttl", "expected"),
    [
        # TDD fixture. Sonnet writes 400k at 2.50 = 1.00; Opus reads 400k at 0.20 = 0.08; 1.00 - 0.08
        (OPUS, SONNET, 400_000, 0, "5m", 0.92),
        # Sonnet writes at 4.00 = 1.60; 1.60 - 0.08
        (OPUS, SONNET, 400_000, 0, "1h", 1.52),
        # fully warm: Sonnet reads 400k at 0.10 = 0.04; 0.04 - 0.08, cheaper even to read
        (OPUS, SONNET, 400_000, 400_000, "1h", -0.04),
        # 100k warm read 0.01 + 300k written at 4.00 = 1.20; 1.21 - 0.08
        (OPUS, SONNET, 400_000, 100_000, "1h", 1.13),
        # warm tokens above the prefix clamp to the prefix: same as fully warm
        (OPUS, SONNET, 400_000, 500_000, "1h", -0.04),
        # negative warm tokens clamp to zero: same as cold
        (OPUS, SONNET, 400_000, -5, "1h", 1.52),
        # Haiku prompt 400k is in the upper tier: write 1.00 x 0.4 = 0.40; 0.40 - 0.08
        (OPUS, HAIKU, 400_000, 0, "1h", 0.32),
        # Haiku prompt 50k is in the lower tier: write 0.125 x 0.05 = 0.00625; Opus reads 0.01
        (OPUS, HAIKU, 50_000, 0, "5m", -0.00375),
        # upgrade: Opus writes 400k at 5.00 = 2.00; Sonnet reads 0.04; 1.96
        (SONNET, OPUS, 400_000, 0, "5m", 1.96),
        # Sonnet writes 1.00; Haiku (upper tier) reads 400k at 0.05 = 0.02; 0.98
        (HAIKU, SONNET, 400_000, 0, "5m", 0.98),
    ],
    ids=[
        "tdd-5m",
        "tdd-1h",
        "fully-warm",
        "partly-warm",
        "warm-over-prefix-clamps",
        "negative-warm-clamps",
        "haiku-upper-tier",
        "haiku-lower-tier",
        "upgrade-costs-more",
        "from-haiku",
    ],
)
def test_switch_tax(
    book: PriceBook, current: str, target: str, prefix: int, warm: int, ttl: Ttl, expected: float
) -> None:
    assert switch_tax(book, current, target, prefix, warm, ttl, DAY) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("current", "target", "prefix", "output", "expected"),
    [
        # 400k x (0.20 - 0.10) = 0.04; 2000 x (20 - 10) = 0.02
        (OPUS, SONNET, 400_000, 2000, 0.06),
        # no output: only the prefix read difference, 0.04
        (OPUS, SONNET, 400_000, 0, 0.04),
        # Haiku upper tier: 400k x (0.20 - 0.05) = 0.06; 2000 x (20 - 2.5) = 0.035
        (OPUS, HAIKU, 400_000, 2000, 0.095),
        # Haiku lower tier: 50k x (0.20 - 0.01) = 0.0095; 2000 x (20 - 0.5) = 0.039
        (OPUS, HAIKU, 50_000, 2000, 0.0485),
        # moving up costs: 400k x (0.10 - 0.20) = -0.04; 2000 x (10 - 20) = -0.02
        (SONNET, OPUS, 400_000, 2000, -0.06),
        (SONNET, SONNET, 400_000, 2000, 0.0),
    ],
    ids=[
        "opus-sonnet",
        "no-output",
        "opus-haiku-upper",
        "opus-haiku-lower",
        "upgrade-negative",
        "same-model",
    ],
)
def test_saving_per_turn(
    book: PriceBook, current: str, target: str, prefix: int, output: float, expected: float
) -> None:
    assert saving_per_turn(book, current, target, prefix, output, DAY) == pytest.approx(expected)


def _policy(cache_ttl_s: int) -> PolicyConfig:
    return PolicyConfig(
        min_confidence=0.6,
        upgrade_confidence=0.75,
        min_turns_between_switches=3,
        margin=0.2,
        cache_ttl_s=cache_ttl_s,
        expected_turns_default=8,
        expected_turns_min_sessions=20,
    )


@pytest.mark.parametrize(
    ("cache_ttl_s", "expected"),
    [(300, "5m"), (3599, "5m"), (3600, "1h"), (7200, "1h")],
    ids=["five-minutes", "just-under-hour", "exactly-hour", "two-hours"],
)
def test_cache_ttl(cache_ttl_s: int, expected: Ttl) -> None:
    assert cache_ttl(_policy(cache_ttl_s)) == expected
