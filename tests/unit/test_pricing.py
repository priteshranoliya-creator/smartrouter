from datetime import date
from pathlib import Path

import pytest
import yaml

from smartrouter.providers.base import Usage
from smartrouter.settings import load_settings
from smartrouter.telemetry.pricing import (
    PriceBook,
    PricingError,
    cost,
    counterfactual,
    load_pricing,
    prefix_cost,
    unpriced,
)

ROOT = Path(__file__).resolve().parents[2]
DAY = date(2026, 10, 19)
MIXED = Usage(
    input_tokens=1000,
    output_tokens=500,
    cache_read_tokens=10_000,
    cache_write_5m_tokens=2000,
    cache_write_1h_tokens=3000,
)


@pytest.fixture(scope="module")
def book() -> PriceBook:
    return load_pricing(ROOT / "config/pricing.yaml")


# Expected values are hand-computed from the published per-million prices.
@pytest.mark.parametrize(
    ("model", "usage", "on", "expected"),
    [
        # 1000*4 + 500*20 + 10000*0.20 + 2000*5 + 3000*8 = 50,000 micro-dollars
        ("claude-opus-5-5", MIXED, DAY, 0.05),
        # 1000*2 + 500*10 + 10000*0.10 + 2000*2.5 + 3000*4
        ("claude-sonnet-5-5", MIXED, DAY, 0.025),
        # prompt 16,000 <= 100k: 1000*0.10 + 500*0.50 + 10000*0.01 + 2000*0.125 + 3000*0.20
        ("claude-haiku-5-5", MIXED, DAY, 0.0013),
        # Haiku 100k boundary is inclusive
        ("claude-haiku-5-5", Usage(input_tokens=100_000), DAY, 0.01),
        ("claude-haiku-5-5", Usage(input_tokens=100_001), DAY, 0.0500005),
        # cache reads count toward the Haiku prompt length: 99,000 + 1,001 > 100k
        ("claude-haiku-5-5", Usage(input_tokens=99_000, cache_read_tokens=1001), DAY, 0.04955005),
        # Gemini Pro 200k boundary, output priced at the prompt's tier
        ("gemini-3.1-pro-preview", Usage(input_tokens=200_000, output_tokens=1000), DAY, 0.412),
        ("gemini-3.1-pro-preview", Usage(input_tokens=200_001, output_tokens=1000), DAY, 0.818004),
        # Gemini cache writes carry no premium
        ("gemini-2.5-flash", Usage(cache_write_5m_tokens=1000), DAY, 0.0003),
        # dated price change
        ("gemini-4-argon", Usage(input_tokens=1_000_000), date(2026, 11, 30), 2.00),
        ("gemini-4-argon", Usage(input_tokens=1_000_000), date(2026, 12, 1), 4.00),
        ("gemini-3.8-flash", Usage(output_tokens=1_000_000), date(2027, 1, 1), 7.50),
        # US-only inference bills 1.1x
        ("claude-opus-5-5", Usage(input_tokens=1_000_000, inference_geo="us"), DAY, 4.40),
        ("claude-opus-5-5", Usage(input_tokens=1_000_000, inference_geo="global"), DAY, 4.00),
    ],
)
def test_cost(book: PriceBook, model: str, usage: Usage, on: date, expected: float) -> None:
    assert cost(book, model, usage, on) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("ttl", "expected_tax"),
    [
        # TDD section 8 fixture: 400k prefix, Opus -> Sonnet. Stay 400k*0.20 = $0.08, switch 400k*2.50 = $1.00
        ("5m", 0.92),
        # same switch on the 1h cache Claude Code uses here: 400k*4.00 = $1.60
        ("1h", 1.52),
    ],
)
def test_switch_tax_fixture(book: PriceBook, ttl: str, expected_tax: float) -> None:
    stay = prefix_cost(book, "claude-opus-5-5", 400_000, warm=True, ttl="5m", on=DAY)
    switch = prefix_cost(book, "claude-sonnet-5-5", 400_000, warm=False, ttl=ttl, on=DAY)  # type: ignore[arg-type]
    assert stay == pytest.approx(0.08)
    assert switch - stay == pytest.approx(expected_tax)


def test_counterfactual_prices_the_asked_model_with_its_warm_prefix(book: PriceBook) -> None:
    # Claude Code asked for Opus; the router sent Sonnet, which had to write the whole prefix.
    sent = Usage(input_tokens=2, cache_write_1h_tokens=36_000, output_tokens=100)
    assert cost(book, "claude-sonnet-5-5", sent, DAY) == pytest.approx(0.145004)
    # Opus had 35k warm: 35000*0.20 read + 1000*8 write + 2*4 input + 100*20 output
    assert counterfactual(book, "claude-opus-5-5", sent, 35_000, DAY) == pytest.approx(0.017008)
    # nothing warm: the whole cacheable part is a write
    assert counterfactual(book, "claude-opus-5-5", sent, 0, DAY) == pytest.approx(0.290008)


def test_unknown_model_raises(book: PriceBook) -> None:
    with pytest.raises(PricingError, match="no price for model 'gpt-x'"):
        cost(book, "gpt-x", Usage(input_tokens=1), DAY)


def test_date_outside_every_period_raises(tmp_path: Path) -> None:
    entry = {
        "provider": "anthropic",
        "verified": True,
        "periods": [{"from": "2026-12-01", "input": 1, "cache_read": 0.1, "output": 5}],
    }
    path = tmp_path / "pricing.yaml"
    path.write_text(yaml.safe_dump({**VALID, "models": {"m": entry}}))
    with pytest.raises(PricingError, match="on 2026-11-30"):
        cost(load_pricing(path), "m", Usage(input_tokens=1), date(2026, 11, 30))


def test_every_configured_model_is_priced(book: PriceBook) -> None:
    router = load_settings(env={}).router
    models = [t.model for t in router.tiers.values()] + list(router.eval.arms.values())
    assert unpriced(book, models) == []


VALID = {
    "providers": {
        "anthropic": {
            "source": "https://example.com",
            "checked_on": "2026-10-19",
            "cache_write_multiplier": {"5m": 1.25, "1h": 2.0},
        }
    },
    "models": {"m": {"provider": "anthropic", "verified": True, "input": 1, "cache_read": 0.1, "output": 5}},
}


@pytest.mark.parametrize(
    ("model_entry", "message"),
    [
        (
            {"provider": "nope", "verified": True, "input": 1, "cache_read": 0.1, "output": 5},
            "unknown provider",
        ),
        ({"provider": "anthropic", "verified": True, "input": -1, "cache_read": 0.1, "output": 5}, "input"),
        (
            {
                "provider": "anthropic",
                "verified": True,
                "tiers": [
                    {"max_prompt_tokens": None, "input": 1, "cache_read": 0.1, "output": 5},
                    {"max_prompt_tokens": 100, "input": 2, "cache_read": 0.2, "output": 9},
                ],
            },
            "only the last tier",
        ),
        (
            {
                "provider": "anthropic",
                "verified": True,
                "periods": [
                    {"until": "2026-12-31", "input": 1, "cache_read": 0.1, "output": 5},
                    {"from": "2026-12-01", "input": 2, "cache_read": 0.2, "output": 9},
                ],
            },
            "must not overlap",
        ),
        ({"provider": "anthropic", "input": 1, "cache_read": 0.1, "output": 5}, "verified"),
        (
            {
                "provider": "anthropic",
                "verified": True,
                "input": 1,
                "cache_read": 0.1,
                "output": 5,
                "typo": 1,
            },
            "typo",
        ),
    ],
)
def test_invalid_pricing_fails_fast(tmp_path: Path, model_entry: dict[str, object], message: str) -> None:
    path = tmp_path / "pricing.yaml"
    path.write_text(yaml.safe_dump({**VALID, "models": {"m": model_entry}}))
    with pytest.raises(PricingError, match=message):
        load_pricing(path)


def test_valid_minimal_pricing_loads(tmp_path: Path) -> None:
    path = tmp_path / "pricing.yaml"
    path.write_text(yaml.safe_dump(VALID))
    assert cost(load_pricing(path), "m", Usage(input_tokens=1_000_000), DAY) == pytest.approx(1.0)
