from datetime import UTC, datetime
from typing import Any

import pytest

from smartrouter.telemetry import stats
from smartrouter.telemetry.models import Decision, DecisionFact, Request, RequestRow, Turn

TIER_MAP = {"A": "haiku", "B": "sonnet", "C": "sonnet", "D": "opus"}


def row(
    cost: float,
    counterfactual: float | None = None,
    mode: Any = None,
    **tokens: int | None,
) -> RequestRow:
    return RequestRow(
        request=Request(
            kind="main",
            requested_model="m",
            sent_model="m",
            provider="anthropic",
            cost_usd=cost,
            counterfactual_cost_usd=counterfactual,
            created_at="2026-10-19T10:00:00+00:00",
            **tokens,
        ),
        mode=mode,
    )


def fact(chosen: str, asked: str | None) -> DecisionFact:
    return DecisionFact(
        decision=Decision(
            turn_id=1, engine="e", answers={}, chosen_model=chosen, rule="same_tier", mode="shadow"
        ),
        turn=Turn(id=1, session_id="s", seq=1, prompt="p", created_at="2026-10-19T10:00:00+00:00"),
        first_main_requested_model=asked,
    )


@pytest.mark.parametrize(
    ("request_row", "expected"),
    [
        pytest.param(row(1.0, 0.4, "shadow"), (1.0, 0.4), id="shadow-with-counterfactual"),
        pytest.param(row(1.0, None, "shadow"), (1.0, 1.0), id="shadow-without-counterfactual"),
        pytest.param(row(1.0, 0.4, None), (1.0, 0.4), id="no-decision-with-counterfactual"),
        pytest.param(row(1.0, None, None), (1.0, 1.0), id="no-decision"),
        pytest.param(row(1.0, 4.0, "active"), (4.0, 1.0), id="active-with-counterfactual"),
        pytest.param(row(1.0, None, "active"), (1.0, 1.0), id="active-without-counterfactual"),
        pytest.param(row(1.0, 0.0, "shadow"), (1.0, 0.0), id="zero-counterfactual-is-set"),
        pytest.param(row(1.0, 0.0, "active"), (0.0, 1.0), id="zero-counterfactual-active"),
    ],
)
def test_request_costs(request_row: RequestRow, expected: tuple[float, float]) -> None:
    assert stats.request_costs(request_row) == expected


def test_totals_sum_requests_and_saving() -> None:
    total = stats.totals([row(1.0, 0.4, "shadow"), row(1.0, 4.0, "active"), row(0.5)])
    assert total.requests == 3
    assert total.cost_usd == pytest.approx(2.5)
    assert total.without_router_usd == pytest.approx(5.5)
    assert total.with_router_usd == pytest.approx(1.9)
    assert total.saving_usd == pytest.approx(3.6)
    assert stats.totals([]) == stats.Totals(0, 0.0, 0.0, 0.0)


@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        ((0, 0, 0, 0), 0.0),
        ((100, 0, 0, 0), 0.0),
        ((0, 100, 0, 0), 1.0),
        ((100, 300, 0, 0), 0.75),
        ((100, 300, 50, 50), 0.6),
        ((0, 0, 10, 10), 0.0),
    ],
)
def test_cache_hit_ratio(tokens: tuple[int, int, int, int], expected: float) -> None:
    assert stats.cache_hit_ratio(*tokens) == pytest.approx(expected)


def test_request_cache_hit_ratio_treats_missing_usage_as_zero() -> None:
    rows = [
        row(1, input_tokens=100, cache_read_tokens=300).request,
        row(1).request,
        row(1, cache_write_1h_tokens=100).request,
    ]
    assert stats.request_cache_hit_ratio(rows) == pytest.approx(300 / 500)
    assert stats.request_cache_hit_ratio([]) == 0.0


@pytest.mark.parametrize(
    ("values", "q", "expected"),
    [
        ([], 0.5, None),
        ([7.0], 0.5, 7.0),
        ([7.0], 0.95, 7.0),
        ([1.0, 2.0], 0.5, 1.5),
        ([3.0, 1.0, 2.0], 0.5, 2.0),
        ([1.0, 2.0, 3.0, 4.0], 0.5, 2.5),
        ([500.0, 1000.0, 1500.0, 2000.0, 3000.0], 0.95, 2800.0),
        ([10.0, 20.0], 0.0, 10.0),
        ([10.0, 20.0], 1.0, 20.0),
    ],
)
def test_percentile_interpolates_linearly(values: list[float], q: float, expected: float | None) -> None:
    assert stats.percentile(values, q) == (None if expected is None else pytest.approx(expected))


@pytest.mark.parametrize(
    ("chosen", "asked", "expected"),
    [
        ("haiku", "sonnet", True),
        ("sonnet", "sonnet", False),
        ("sonnet", None, False),
    ],
)
def test_is_switch(chosen: str, asked: str | None, expected: bool) -> None:
    assert stats.is_switch(fact(chosen, asked)) is expected


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        ({}, None),
        ({"other": {"choice": "A", "probabilities": {"A": 1.0}}}, None),
        ({"complexity": {"choice": "A", "probabilities": {"A": 0.8, "B": 0.1, "C": 0.05, "D": 0.05}}}, 0.8),
        # B and C share a tier, so their probabilities add up to the winning tier's probability
        ({"complexity": {"choice": "C", "probabilities": {"A": 0.2, "B": 0.3, "C": 0.35, "D": 0.15}}}, 0.65),
        ({"complexity": {"choice": "D", "probabilities": {"A": 0.25, "B": 0.25, "C": 0.25, "D": 0.25}}}, 0.5),
    ],
)
def test_tier_probability(answers: dict[str, Any], expected: float | None) -> None:
    got = stats.tier_probability(answers, "complexity", TIER_MAP)
    assert got == (None if expected is None else pytest.approx(expected))


@pytest.mark.parametrize(
    ("probability", "bin_index"),
    [(0.0, 0), (0.05, 0), (0.1, 1), (0.3, 3), (0.55, 5), (0.7, 7), (0.9, 9), (0.95, 9), (1.0, 9)],
)
def test_histogram_bin(probability: float, bin_index: int) -> None:
    assert stats.histogram_bin(probability) == bin_index


@pytest.mark.parametrize(
    ("created_at", "hourly", "expected"),
    [
        ("2026-10-19T14:35:12.5+00:00", True, "2026-10-19T14:00:00+00:00"),
        ("2026-10-19T14:35:12.5+00:00", False, "2026-10-19T00:00:00+00:00"),
        ("2026-10-19T23:59:59+00:00", True, "2026-10-19T23:00:00+00:00"),
        ("2026-10-19T01:30:00+02:00", True, "2026-10-18T23:00:00+00:00"),
    ],
)
def test_bucket(created_at: str, hourly: bool, expected: str) -> None:
    assert stats.bucket(created_at, hourly) == expected


NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        ("15m", "2026-10-19T11:45:00+00:00"),
        ("1h", "2026-10-19T11:00:00+00:00"),
        ("24h", "2026-10-18T12:00:00+00:00"),
        ("7d", "2026-10-12T12:00:00+00:00"),
        ("30d", "2026-09-19T12:00:00+00:00"),
        ("all", None),
    ],
)
def test_window_since(window: str, expected: str | None) -> None:
    assert stats.window_since(window, NOW) == expected


@pytest.mark.parametrize("window", ["2h", "", "ALL", "1d"])
def test_window_since_rejects_unknown_windows(window: str) -> None:
    with pytest.raises(ValueError, match="unknown window"):
        stats.window_since(window, NOW)
