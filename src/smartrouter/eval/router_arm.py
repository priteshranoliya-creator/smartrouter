"""The `router` eval arm: the decision engine plus the policy, as a fresh single-turn session."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from smartrouter.decision.engine import DecisionEngine, EngineError
from smartrouter.decision.features import build_state
from smartrouter.eval.dataset import GoldenItem
from smartrouter.policy.cost_estimator import tier_probabilities
from smartrouter.policy.engine import decide_policy, expected_turns
from smartrouter.policy.rules import PolicyContext, PolicyInput
from smartrouter.settings import Settings


@dataclass(frozen=True)
class Prediction:
    """`tier` is what the router serves: the policy's choice, or the fallback when it abstained."""

    tier: str
    desired_tier: str
    rule: str
    fell_back: bool


def item_text(item: GoldenItem) -> str:
    return f"{item.context}\n\n{item.prompt}" if item.context else item.prompt


class RouterArm:
    def __init__(self, engine: DecisionEngine, settings: Settings, context: PolicyContext) -> None:
        router = settings.router
        self._engine = engine
        self._question = router.decision.question
        self._tier_map = router.decision.tier_map
        self._context = context
        self._fallback = router.eval.router_fallback_tier
        self._expected_turns = expected_turns(None, 0, router.policy)

    async def predict(self, item: GoldenItem) -> Prediction:
        decision = await self._engine.decide(
            build_state(item_text(item), None, self._engine.state_budget_tokens)
        )
        answer = decision.answers.get(self._question)
        if answer is None:
            raise EngineError(f"{decision.engine}: no answer for question {self._question!r}")
        probabilities = tier_probabilities(answer, self._tier_map)
        desired = max(probabilities, key=lambda tier: probabilities[tier])
        policy = decide_policy(
            PolicyInput(
                desired_tier=desired,
                tier_probability=probabilities[desired],
                current_tier=None,
                override=None,
                prefix_tokens=0,
                warm_tokens_by_tier={},
                seconds_since_last_request=None,
                just_compacted=False,
                turns_since_switch=None,
                est_output_tokens=0.0,
                expected_turns=self._expected_turns,
                on=datetime.now(UTC).date(),
            ),
            self._context,
        )
        chosen = policy.chosen_tier
        return Prediction(
            tier=self._fallback if chosen is None else chosen,
            desired_tier=desired,
            rule=policy.rule,
            fell_back=chosen is None,
        )
