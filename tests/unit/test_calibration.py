import math

import pytest

from smartrouter.decision.calibration import (
    MAX_TEMPERATURE,
    MIN_TEMPERATURE,
    accuracy,
    apply_temperature,
    confusion,
    ece,
    fit_temperature,
    nll,
    sum_by_tier,
    tier_nll,
)

TIER_MAP = {"A": "haiku", "B": "sonnet", "C": "sonnet", "D": "opus"}


def test_temperature_one_leaves_probabilities_unchanged() -> None:
    probs = {"A": 0.5, "B": 0.3, "C": 0.15, "D": 0.05}
    assert apply_temperature(probs, 1.0) == pytest.approx(probs, abs=1e-6)


def test_temperature_two_takes_the_square_root_and_renormalizes() -> None:
    scaled = apply_temperature({"A": 0.64, "B": 0.36}, 2.0)
    assert scaled["A"] == pytest.approx(0.8 / 1.4, abs=1e-6)
    assert scaled["B"] == pytest.approx(0.6 / 1.4, abs=1e-6)
    assert sum(scaled.values()) == pytest.approx(1.0)


def test_temperature_half_squares_and_renormalizes() -> None:
    scaled = apply_temperature({"A": 0.5, "B": 0.3, "C": 0.1, "D": 0.1}, 0.5)
    assert scaled["A"] == pytest.approx(0.25 / 0.36, abs=1e-5)
    assert scaled["B"] == pytest.approx(0.09 / 0.36, abs=1e-5)


def test_sum_by_tier_adds_labels_of_one_tier() -> None:
    assert sum_by_tier({"A": 0.1, "B": 0.3, "C": 0.2, "D": 0.4}, TIER_MAP) == pytest.approx(
        {"haiku": 0.1, "sonnet": 0.5, "opus": 0.4}
    )


def test_nll_is_the_mean_negative_log_of_the_expected_tier() -> None:
    value = nll([{"haiku": 0.5}, {"haiku": 0.25, "opus": 0.75}], ["haiku", "opus"])
    assert value == pytest.approx((math.log(2) + math.log(4 / 3)) / 2)


def test_nll_floors_a_missing_tier_instead_of_diverging() -> None:
    assert math.isfinite(nll([{"haiku": 1.0}], ["opus"]))


def test_tier_nll_sums_labels_into_the_expected_tier() -> None:
    samples = [({"A": 0.2, "B": 0.3, "C": 0.2, "D": 0.3}, "sonnet")]
    assert tier_nll(samples, TIER_MAP) == pytest.approx(-math.log(0.5))


def test_accuracy_counts_matches() -> None:
    assert accuracy(["a", "b", "c", "a"], ["a", "b", "a", "a"]) == 0.75


def test_ece_of_a_toy_set_weights_each_bin_by_its_size() -> None:
    # bin 0.9-1.0: two items, accuracy 0.5, confidence 0.95 -> 0.45 * 2/4
    # bin 0.5-0.6: two items, accuracy 1.0, confidence 0.55 -> 0.45 * 2/4
    assert ece([0.95, 0.95, 0.55, 0.55], [True, False, True, True]) == pytest.approx(0.45)


def test_ece_is_zero_when_confidence_matches_accuracy() -> None:
    assert ece([1.0, 1.0], [True, True]) == 0.0
    assert ece([0.5, 0.5], [True, False]) == pytest.approx(0.0)


def test_confusion_has_every_tier_on_both_axes() -> None:
    table = confusion(["a", "a", "b", "c"], ["a", "b", "b", "a"], ["a", "b", "c"])
    assert table == {
        "a": {"a": 1, "b": 1, "c": 0},
        "b": {"a": 0, "b": 1, "c": 0},
        "c": {"a": 1, "b": 0, "c": 0},
    }


def test_separable_samples_push_temperature_below_one() -> None:
    samples = [({"A": 0.6, "B": 0.1, "C": 0.1, "D": 0.2}, "haiku")] * 5
    samples += [({"A": 0.1, "B": 0.1, "C": 0.1, "D": 0.7}, "opus")] * 5
    temperature = fit_temperature(samples, TIER_MAP)
    assert temperature < 1
    assert tier_nll(samples, TIER_MAP, temperature) < tier_nll(samples, TIER_MAP)


def test_overconfident_wrong_answers_push_temperature_above_one() -> None:
    right = ({"A": 0.9, "B": 0.05, "C": 0.03, "D": 0.02}, "haiku")
    wrong = ({"A": 0.9, "B": 0.05, "C": 0.03, "D": 0.02}, "opus")
    temperature = fit_temperature([right, wrong], TIER_MAP)
    assert temperature > 1
    assert tier_nll([right, wrong], TIER_MAP, temperature) < tier_nll([right, wrong], TIER_MAP)


def test_already_calibrated_samples_fit_temperature_one() -> None:
    probs = {"A": 0.8, "B": 0.0, "C": 0.0, "D": 0.2}
    samples = [(probs, "haiku")] * 8 + [(probs, "opus")] * 2
    assert fit_temperature(samples, TIER_MAP) == pytest.approx(1.0, abs=0.01)


def test_temperature_stays_inside_the_search_bounds() -> None:
    always_right = [({"A": 0.55, "B": 0.15, "C": 0.15, "D": 0.15}, "haiku")] * 4
    assert fit_temperature(always_right, TIER_MAP) == pytest.approx(MIN_TEMPERATURE, rel=0.01)
    always_wrong = [({"A": 0.9, "B": 0.05, "C": 0.03, "D": 0.02}, "opus")] * 4
    assert fit_temperature(always_wrong, TIER_MAP) == pytest.approx(MAX_TEMPERATURE, rel=0.01)


def test_empty_inputs_are_rejected() -> None:
    with pytest.raises(ValueError):
        fit_temperature([], TIER_MAP)
    with pytest.raises(ValueError):
        ece([], [])
    with pytest.raises(ValueError):
        accuracy([], [])
