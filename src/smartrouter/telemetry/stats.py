"""Spend, savings and decision statistics. Pure functions: the API and the CSV export share them."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from smartrouter.decision.engine import QuestionAnswer
from smartrouter.policy.cost_estimator import tier_probabilities
from smartrouter.telemetry.models import DecisionFact, Request, RequestRow

WINDOWS: dict[str, timedelta | None] = {
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "all": None,
}
HISTOGRAM_BINS = 10


def window_since(window: str, now: datetime) -> str | None:
    """ISO UTC lower bound of the window, None for `all`. Unknown windows raise ValueError."""
    if window not in WINDOWS:
        raise ValueError(f"unknown window {window!r}; choose one of {', '.join(WINDOWS)}")
    delta = WINDOWS[window]
    return None if delta is None else (now.astimezone(UTC) - delta).isoformat()


def request_costs(row: RequestRow) -> tuple[float, float]:
    """(without_router_usd, with_router_usd) of one request.

    Shadow or no decision: the request ran as asked, the counterfactual is what routing would cost.
    Active: the request ran as routed, the counterfactual is what the asked model would have cost.
    """
    req = row.request
    counterfactual = req.cost_usd if req.counterfactual_cost_usd is None else req.counterfactual_cost_usd
    if row.mode == "active":
        return counterfactual, req.cost_usd
    return req.cost_usd, counterfactual


def cache_hit_ratio(input_tokens: int, cache_read: int, cache_write_5m: int, cache_write_1h: int) -> float:
    total = input_tokens + cache_read + cache_write_5m + cache_write_1h
    return 0.0 if total == 0 else cache_read / total


def request_cache_hit_ratio(requests: Iterable[Request]) -> float:
    return cache_hit_ratio(
        sum(r.input_tokens or 0 for r in requests),
        sum(r.cache_read_tokens or 0 for r in requests),
        sum(r.cache_write_5m_tokens or 0 for r in requests),
        sum(r.cache_write_1h_tokens or 0 for r in requests),
    )


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear interpolation between closest ranks; `q` in [0, 1]. None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * q
    low, high = math.floor(rank), math.ceil(rank)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def is_switch(fact: DecisionFact) -> bool:
    """The router chose another model than the turn's first main request asked for."""
    asked = fact.first_main_requested_model
    return asked is not None and fact.decision.chosen_model != asked


def tier_probability(answers: Mapping[str, Any], question: str, tier_map: Mapping[str, str]) -> float | None:
    """Probability of the most likely tier for the routing question, None when it was not answered."""
    answer = answers.get(question)
    if not answer:
        return None
    probabilities = answer["probabilities"]
    return max(
        tier_probabilities(
            QuestionAnswer(choice=answer["choice"], probabilities=probabilities), tier_map
        ).values()
    )


def histogram_bin(probability: float) -> int:
    """0-0.1 is bin 0 ... 0.9-1.0 is bin 9, which also holds 1.0."""
    return min(math.floor(round(probability * HISTOGRAM_BINS, 9)), HISTOGRAM_BINS - 1)


def bucket(created_at: str, hourly: bool) -> str:
    when = datetime.fromisoformat(created_at).astimezone(UTC)
    when = when.replace(minute=0, second=0, microsecond=0)
    return (when if hourly else when.replace(hour=0)).isoformat()


@dataclass(frozen=True)
class Totals:
    requests: int
    cost_usd: float
    without_router_usd: float
    with_router_usd: float

    @property
    def saving_usd(self) -> float:
        return self.without_router_usd - self.with_router_usd


def totals(rows: Iterable[RequestRow]) -> Totals:
    count, spend, without, with_router = 0, 0.0, 0.0, 0.0
    for row in rows:
        costs = request_costs(row)
        count += 1
        spend += row.request.cost_usd
        without += costs[0]
        with_router += costs[1]
    return Totals(count, spend, without, with_router)


def _cache_write(request: Request) -> int:
    return (request.cache_write_5m_tokens or 0) + (request.cache_write_1h_tokens or 0)


def _by_model(rows: Sequence[RequestRow]) -> list[dict[str, Any]]:
    groups: dict[str, list[RequestRow]] = defaultdict(list)
    for row in rows:
        groups[row.request.sent_model].append(row)
    out = []
    for model, group in groups.items():
        requests = [r.request for r in group]
        out.append(
            {
                "model": model,
                "turns": len({r.turn_id for r in requests if r.turn_id is not None}),
                "requests": len(requests),
                "spend_usd": sum(r.cost_usd for r in requests),
                "input_tokens": sum(r.input_tokens or 0 for r in requests),
                "cache_read_tokens": sum(r.cache_read_tokens or 0 for r in requests),
                "cache_write_tokens": sum(_cache_write(r) for r in requests),
                "cache_hit_ratio": request_cache_hit_ratio(requests),
            }
        )
    return sorted(out, key=lambda m: (-m["spend_usd"], m["model"]))


def _timeseries(rows: Sequence[RequestRow], hourly: bool) -> list[dict[str, Any]]:
    groups: dict[str, list[RequestRow]] = defaultdict(list)
    for row in rows:
        groups[bucket(row.request.created_at, hourly)].append(row)
    out = []
    for key in sorted(groups):
        group = groups[key]
        total = totals(group)
        out.append(
            {
                "bucket": key,
                "spend_usd": total.cost_usd,
                "without_router_usd": total.without_router_usd,
                "with_router_usd": total.with_router_usd,
                "cache_write_tokens": sum(_cache_write(r.request) for r in group),
            }
        )
    return out


def _switch_ledger(facts: Sequence[DecisionFact], rows: Sequence[RequestRow]) -> list[dict[str, Any]]:
    saving_by_turn: dict[int, float] = defaultdict(float)
    for row in rows:
        if row.request.turn_id is not None:
            without, with_router = request_costs(row)
            saving_by_turn[row.request.turn_id] += without - with_router
    return [
        {
            "turn_id": fact.turn.id,
            "session_id": fact.turn.session_id,
            "seq": fact.turn.seq,
            "created_at": fact.turn.created_at,
            "from_model": fact.first_main_requested_model,
            "to_model": fact.decision.chosen_model,
            "est_switch_tax_usd": fact.decision.est_switch_tax_usd,
            "est_saving_usd": fact.decision.est_saving_usd,
            "realised_saving_usd": saving_by_turn.get(fact.decision.turn_id, 0.0),
        }
        for fact in facts
        if is_switch(fact)
    ]


def compute_stats(
    *,
    window: str,
    since: str | None,
    rows: Sequence[RequestRow],
    facts: Sequence[DecisionFact],
    sessions: int,
    turns: int,
    question: str,
    tier_map: Mapping[str, str],
) -> dict[str, Any]:
    """The body of GET /api/stats. Windows longer than 24 h bucket the timeseries by day."""
    total = totals(rows)
    requests = [r.request for r in rows]
    by_rule = Counter(fact.decision.rule for fact in facts)
    latencies = [f.decision.engine_latency_ms for f in facts if f.decision.engine_latency_ms is not None]
    probabilities = [
        p for f in facts if (p := tier_probability(f.decision.answers, question, tier_map)) is not None
    ]
    bins = [0] * HISTOGRAM_BINS
    for probability in probabilities:
        bins[histogram_bin(probability)] += 1
    window_delta = WINDOWS[window]
    return {
        "window": window,
        "since": since,
        "sessions": sessions,
        "turns": turns,
        "requests": total.requests,
        "spend_usd": total.cost_usd,
        "without_router_usd": total.without_router_usd,
        "with_router_usd": total.with_router_usd,
        "saving_usd": total.saving_usd,
        "cache_hit_ratio": request_cache_hit_ratio(requests),
        "switches": sum(1 for f in facts if is_switch(f)),
        "decisions_by_rule": dict(by_rule),
        "timeouts": by_rule["decision_timeout"],
        "low_confidence": by_rule["low_confidence"],
        "engine_latency_ms": {"p50": percentile(latencies, 0.5), "p95": percentile(latencies, 0.95)},
        "tier_probability_histogram": bins,
        "by_model": _by_model(rows),
        "timeseries": _timeseries(
            rows, hourly=window_delta is not None and window_delta <= timedelta(hours=24)
        ),
        "switch_ledger": _switch_ledger(facts, rows),
    }
