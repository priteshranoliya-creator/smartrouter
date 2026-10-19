"""Policy engine: run the ordered rules and size the expected-turns horizon."""

from __future__ import annotations

from smartrouter.policy.rules import RULES, PolicyContext, PolicyDecision, PolicyInput
from smartrouter.settings import PolicyConfig


def decide_policy(inp: PolicyInput, ctx: PolicyContext) -> PolicyDecision:
    for rule in RULES:
        decision = rule(inp, ctx)
        if decision is not None:
            return decision
    raise RuntimeError("no policy rule decided; free_window handles new sessions, payback is total otherwise")


def expected_turns(median_turns_per_session: float | None, sessions_seen: int, cfg: PolicyConfig) -> float:
    """The configured default until enough sessions are logged, then the logged median."""
    if median_turns_per_session is None or sessions_seen < cfg.expected_turns_min_sessions:
        return float(cfg.expected_turns_default)
    return median_turns_per_session
