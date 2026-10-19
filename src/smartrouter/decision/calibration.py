"""Temperature scaling and the metrics that judge it. Pure maths over probability vectors, no I/O."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

EPSILON = 1e-9
NLL_FLOOR = 1e-12
MIN_TEMPERATURE = 0.05
MAX_TEMPERATURE = 20.0
SEARCH_STEPS = 80
_GOLDEN = (math.sqrt(5) - 1) / 2

Sample = tuple[Mapping[str, float], str]


def sum_by_tier(label_probabilities: Mapping[str, float], tier_map: Mapping[str, str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for label, probability in label_probabilities.items():
        tier = tier_map[label]
        out[tier] = out.get(tier, 0.0) + probability
    return out


def apply_temperature(probabilities: Mapping[str, float], temperature: float) -> dict[str, float]:
    """softmax(log(p + 1e-9) / T). T above 1 flattens, below 1 sharpens, 1 leaves p unchanged."""
    logits = {k: math.log(p + EPSILON) / temperature for k, p in probabilities.items()}
    peak = max(logits.values())
    weights = {k: math.exp(v - peak) for k, v in logits.items()}
    total = sum(weights.values())
    return {k: w / total for k, w in weights.items()}


def nll(tier_probabilities: Sequence[Mapping[str, float]], expected: Sequence[str]) -> float:
    """Mean negative log probability assigned to the expected tier."""
    if not expected:
        raise ValueError("nll needs at least one sample")
    losses = (
        -math.log(max(p.get(e, 0.0), NLL_FLOOR)) for p, e in zip(tier_probabilities, expected, strict=True)
    )
    return sum(losses) / len(expected)


def tier_nll(
    samples: Sequence[Sample], tier_map: Mapping[str, str], temperature: float | None = None
) -> float:
    """NLL of the expected tier over `samples`. `temperature` None scores the raw probabilities."""
    scored = [
        sum_by_tier(labels if temperature is None else apply_temperature(labels, temperature), tier_map)
        for labels, _ in samples
    ]
    return nll(scored, [expected for _, expected in samples])


def fit_temperature(samples: Sequence[Sample], tier_map: Mapping[str, str]) -> float:
    """The T in [0.05, 20] that minimizes mean NLL of the expected tier (golden-section on log T)."""
    if not samples:
        raise ValueError("fit_temperature needs at least one sample")

    def loss(log_t: float) -> float:
        return tier_nll(samples, tier_map, math.exp(log_t))

    low, high = math.log(MIN_TEMPERATURE), math.log(MAX_TEMPERATURE)
    a = high - _GOLDEN * (high - low)
    b = low + _GOLDEN * (high - low)
    loss_a, loss_b = loss(a), loss(b)
    for _ in range(SEARCH_STEPS):
        if loss_a <= loss_b:
            high, b, loss_b = b, a, loss_a
            a = high - _GOLDEN * (high - low)
            loss_a = loss(a)
        else:
            low, a, loss_a = a, b, loss_b
            b = low + _GOLDEN * (high - low)
            loss_b = loss(b)
    return math.exp((low + high) / 2)


def accuracy(expected: Sequence[str], predicted: Sequence[str]) -> float:
    if not expected:
        raise ValueError("accuracy needs at least one sample")
    return sum(e == p for e, p in zip(expected, predicted, strict=True)) / len(expected)


def ece(confidences: Sequence[float], correct: Sequence[bool], bins: int = 10) -> float:
    """Expected calibration error: equal-width confidence bins, |accuracy - confidence| weighted by size."""
    if not confidences:
        raise ValueError("ece needs at least one sample")
    members: list[list[int]] = [[] for _ in range(bins)]
    for i, confidence in enumerate(confidences):
        members[min(int(confidence * bins), bins - 1)].append(i)
    total = len(confidences)
    error = 0.0
    for bucket in members:
        if bucket:
            acc = sum(correct[i] for i in bucket) / len(bucket)
            conf = sum(confidences[i] for i in bucket) / len(bucket)
            error += len(bucket) / total * abs(acc - conf)
    return error


def confusion(
    expected: Sequence[str], predicted: Sequence[str], tiers: Sequence[str]
) -> dict[str, dict[str, int]]:
    """Counts by [expected][predicted], every tier present in both axes."""
    table = {e: dict.fromkeys(tiers, 0) for e in tiers}
    for e, p in zip(expected, predicted, strict=True):
        table[e][p] += 1
    return table
