"""Per-turn decisions: engine, then policy, run in the background and joined by the proxy.

`start` is called when the prompt hook fires. The proxy never waits for it: in active mode a
request uses the decision only once it is `ready` (tdd-changes #28), and the recorder awaits
`outcome` after the request has been forwarded. The decision row itself is written by the
recorder (see `DecisionService.decision_row`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from smartrouter.decision.engine import Decision, DecisionEngine, EngineError
from smartrouter.decision.features import SessionView, build_state
from smartrouter.policy.cost_estimator import tier_probabilities
from smartrouter.policy.engine import decide_policy, expected_turns
from smartrouter.policy.rules import PolicyContext, PolicyDecision, PolicyInput
from smartrouter.settings import Settings
from smartrouter.telemetry.models import Decision as DecisionRow
from smartrouter.telemetry.models import Mode, Override, Session, Turn
from smartrouter.telemetry.pricing import PriceBook
from smartrouter.telemetry.repo import Repo

logger = logging.getLogger(__name__)

TASK_TTL_S = 600.0


@dataclass(frozen=True)
class Outcome:
    decision: Decision
    policy: PolicyDecision
    desired_model: str
    chosen_model: str | None


@dataclass(frozen=True)
class PolicyFacts:
    """What the policy needs to know about the session, beyond the engine's answer."""

    current_tier: str | None = None
    prefix_tokens: int = 0
    warm_tokens_by_tier: Mapping[str, int] | None = None
    seconds_since_last_request: float | None = None
    just_compacted: bool = False
    turns_since_switch: int | None = None
    est_output_tokens: float = 0.0
    expected_turns: float = 1.0


@dataclass
class _Pending:
    task: asyncio.Task[Outcome | None]
    due_at: float
    expiry: asyncio.TimerHandle


def _seconds_since(then: str, now: datetime) -> float:
    return (now - datetime.fromisoformat(then)).total_seconds()


class DecisionService:
    def __init__(self, engine: DecisionEngine, settings: Settings, book: PriceBook, repo: Repo) -> None:
        self._engine = engine
        self._settings = settings
        self._repo = repo
        router = settings.router
        self._tiers = router.tiers
        self._tier_by_model = {tier.model: name for name, tier in router.tiers.items()}
        self._context = PolicyContext(config=router.policy, tiers=router.tiers, book=book)
        self._deadline_s = router.decision.deadline_ms / 1000
        self._pending: dict[int, _Pending] = {}
        self.mode: Mode = settings.router.mode.value

    @property
    def cache_ttl_s(self) -> int:
        return self._settings.router.policy.cache_ttl_s

    def start(self, turn: Turn, session: Session) -> None:
        assert turn.id is not None
        loop = asyncio.get_running_loop()
        turn_id = turn.id
        self._pending[turn_id] = _Pending(
            task=loop.create_task(self._run(turn, session)),
            due_at=loop.time() + self._deadline_s,
            expiry=loop.call_later(TASK_TTL_S, self.release, turn_id),
        )

    async def outcome(self, turn_id: int) -> Outcome | None:
        """The turn's decision, waiting until the deadline (counted from `start`) at most.

        None when the turn is unknown, the engine failed, or the deadline passed.
        """
        pending = self._pending.get(turn_id)
        if pending is None:
            return None
        if pending.task.done():
            return pending.task.result()
        remaining = max(pending.due_at - asyncio.get_running_loop().time(), 0.0)
        try:
            return await asyncio.wait_for(asyncio.shield(pending.task), remaining)
        except TimeoutError:
            return None

    def ready(self, turn_id: int) -> Outcome | None:
        """The turn's decision if the engine has already answered. Never waits."""
        pending = self._pending.get(turn_id)
        if pending is None or not pending.task.done() or pending.task.cancelled():
            return None
        return pending.task.result()

    def failed(self, turn_id: int) -> bool:
        """True when the engine has already given up on the turn (an error, not a slow answer)."""
        pending = self._pending.get(turn_id)
        return (
            pending is not None
            and pending.task.done()
            and not pending.task.cancelled()
            and pending.task.result() is None
        )

    def tier_model(self, tier: str) -> str | None:
        """The model of a tier name (an override like `sonnet`), None for anything else."""
        config = self._tiers.get(tier)
        return None if config is None else config.model

    def is_tier_model(self, model: str | None) -> bool:
        return model in self._tier_by_model

    def release(self, turn_id: int) -> None:
        pending = self._pending.pop(turn_id, None)
        if pending is None:
            return
        pending.expiry.cancel()
        pending.task.cancel()

    async def aclose(self) -> None:
        tasks = [p.task for p in self._pending.values()]
        for turn_id in list(self._pending):
            self.release(turn_id)
        await asyncio.gather(*tasks, return_exceptions=True)

    def decision_row(self, turn_id: int, outcome: Outcome | None, standing_model: str) -> DecisionRow:
        """The turn's decisions row. With no outcome the model the turn's first request ran on stands
        and the rule is decision_error if the engine failed, else decision_timeout."""
        if outcome is None:
            return DecisionRow(
                turn_id=turn_id,
                engine=self._engine.name,
                answers={},
                chosen_model=standing_model,
                rule="decision_error" if self.failed(turn_id) else "decision_timeout",
                mode=self.mode,
            )
        return DecisionRow(
            turn_id=turn_id,
            engine=outcome.decision.engine,
            answers={
                question: {"choice": answer.choice, "probabilities": dict(answer.probabilities)}
                for question, answer in outcome.decision.answers.items()
            },
            engine_latency_ms=outcome.decision.latency_ms,
            desired_model=outcome.desired_model,
            chosen_model=outcome.chosen_model or standing_model,
            rule=outcome.policy.rule,
            mode=self.mode,
            est_switch_tax_usd=outcome.policy.est_switch_tax_usd,
            est_saving_usd=outcome.policy.est_saving_usd,
        )

    async def preview(self, prompt: str, current_model: str | None, override: Override | None) -> Outcome:
        """A decision for a brand-new session that has run `current_model`. Reads and writes nothing."""
        tier = None if current_model is None else self._tier_by_model.get(current_model)
        view = SessionView(
            turn_seq=1,
            current_tier=tier,
            minutes_since_last_request=None,
            just_compacted=False,
            previous_prompt=None,
        )
        facts = PolicyFacts(
            current_tier=tier,
            expected_turns=expected_turns(None, 0, self._settings.router.policy),
        )
        return await self._evaluate(prompt, override, view, facts)

    async def _run(self, turn: Turn, session: Session) -> Outcome | None:
        try:
            view, facts = self._gather(turn, session)
            return await self._evaluate(turn.prompt, turn.override, view, facts)
        except EngineError as exc:
            logger.warning("decision for turn %s failed: %s", turn.id, exc)
        except Exception:
            logger.exception("decision for turn %s failed", turn.id)
        return None

    def _gather(self, turn: Turn, session: Session) -> tuple[SessionView, PolicyFacts]:
        now = datetime.now(UTC)
        tier = None if session.current_model is None else self._tier_by_model.get(session.current_model)
        since_last = None if session.last_request_at is None else _seconds_since(session.last_request_at, now)
        just_compacted = session.state == "compacting"
        view = SessionView(
            turn_seq=turn.seq,
            current_tier=tier,
            minutes_since_last_request=None if since_last is None else since_last / 60,
            just_compacted=just_compacted,
            previous_prompt=self._repo.previous_turn_prompt(session.id, turn.seq),
        )
        warm_rows = self._repo.get_session_models(session.id)
        prefix = next((r.prefix_tokens or 0 for r in warm_rows if r.model == session.current_model), 0)
        ttl = self.cache_ttl_s
        warm = {
            self._tier_by_model[r.model]: r.prefix_tokens or 0
            for r in warm_rows
            if r.model in self._tier_by_model and _seconds_since(r.last_used_at, now) <= ttl
        }
        median, sessions_seen = self._repo.turns_per_session()
        facts = PolicyFacts(
            current_tier=tier,
            prefix_tokens=prefix,
            warm_tokens_by_tier=warm,
            seconds_since_last_request=since_last,
            just_compacted=just_compacted,
            turns_since_switch=None
            if session.last_switch_turn is None
            else turn.seq - session.last_switch_turn,
            est_output_tokens=self._repo.mean_main_output_tokens(session.id) or 0.0,
            expected_turns=expected_turns(median, sessions_seen, self._settings.router.policy),
        )
        return view, facts

    async def _evaluate(
        self, prompt: str, override: Override | None, view: SessionView | None, facts: PolicyFacts
    ) -> Outcome:
        question = self._settings.router.decision.question
        decision = await self._engine.decide(build_state(prompt, view, self._engine.state_budget_tokens))
        answer = decision.answers.get(question)
        if answer is None:
            raise EngineError(f"{decision.engine}: no answer for question {question!r}")
        probabilities = tier_probabilities(answer, self._settings.router.decision.tier_map)
        desired = max(probabilities, key=lambda tier: probabilities[tier])
        policy = decide_policy(
            PolicyInput(
                desired_tier=desired,
                tier_probability=probabilities[desired],
                current_tier=facts.current_tier,
                override=override,
                prefix_tokens=facts.prefix_tokens,
                warm_tokens_by_tier=facts.warm_tokens_by_tier or {},
                seconds_since_last_request=facts.seconds_since_last_request,
                just_compacted=facts.just_compacted,
                turns_since_switch=facts.turns_since_switch,
                est_output_tokens=facts.est_output_tokens,
                expected_turns=facts.expected_turns,
                on=datetime.now(UTC).date(),
            ),
            self._context,
        )
        return Outcome(
            decision=decision,
            policy=policy,
            desired_model=self._tiers[policy.desired_tier].model,
            chosen_model=None if policy.chosen_tier is None else self._tiers[policy.chosen_tier].model,
        )
