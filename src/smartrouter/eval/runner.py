"""Eval runner: answer and judge every (item, arm) pair, storing one eval_results row each."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Generic, TypeVar

from smartrouter.decision.engine import EngineError
from smartrouter.eval.dataset import GoldenItem
from smartrouter.eval.judge import Judgment, in_rejudge_sample, judge_answer
from smartrouter.eval.providers import Arms, EvalError, EvalProvider, render_prompt
from smartrouter.eval.router_arm import Prediction, RouterArm
from smartrouter.telemetry.models import EvalResult, EvalRun
from smartrouter.telemetry.repo import Repo

ROUTER_ARM = "router"
ORACLE_ARM = "oracle"
VIRTUAL_ARMS = (ROUTER_ARM, ORACLE_ARM)

K = TypeVar("K")
V = TypeVar("V")


class RunSetupError(Exception):
    """The requested run cannot start (unknown arm, bad resume)."""


@dataclass(frozen=True)
class Produced:
    """One model's answer to one item, with the cost and latency of producing it."""

    text: str
    model: str
    cost_usd: float
    latency_ms: float


@dataclass(frozen=True)
class RunnerConfig:
    arms: Arms  # fixed arms: name -> (provider, model id)
    tier_models: Mapping[str, str]  # tier -> model id, for the router and oracle arms
    tier_provider: EvalProvider  # answers for a tier's model that no fixed arm covers
    judge: tuple[EvalProvider, str]
    rejudge: tuple[EvalProvider, str] | None
    rejudge_fraction: float
    router: RouterArm | None = None
    concurrency: int = 4


class _Once(Generic[K, V]):
    """Run each keyed job once; concurrent and later callers share its result or its error."""

    def __init__(self) -> None:
        self._futures: dict[K, asyncio.Future[V]] = {}

    def seed(self, key: K, value: V) -> None:
        future: asyncio.Future[V] = asyncio.get_running_loop().create_future()
        future.set_result(value)
        self._futures[key] = future

    async def get(self, key: K, make: Callable[[], Awaitable[V]]) -> V:
        future = self._futures.get(key)
        if future is None:
            future = asyncio.ensure_future(make())
            self._futures[key] = future
        return await future


class EvalRunner:
    def __init__(
        self, repo: Repo, config: RunnerConfig, progress: Callable[[EvalResult], None] | None = None
    ) -> None:
        self._repo = repo
        self._cfg = config
        self._progress = progress
        self._answers: _Once[tuple[str, str], Produced] = _Once()
        self._judgments: _Once[tuple[str, str], Judgment] = _Once()
        self._predictions: _Once[str, Prediction] = _Once()

    def check_arms(self, arm_names: Sequence[str]) -> None:
        known = [*self._cfg.arms, *VIRTUAL_ARMS]
        if unknown := [a for a in arm_names if a not in known]:
            raise RunSetupError(f"unknown arm(s) {', '.join(unknown)} (known: {', '.join(known)})")
        if ROUTER_ARM in arm_names and self._cfg.router is None:
            raise RunSetupError("the router arm needs a decision engine")

    async def run(
        self,
        run_id: int,
        items: Sequence[GoldenItem],
        arm_names: Sequence[str],
        done: Collection[tuple[str, str]] = frozenset(),
        previous: Sequence[EvalResult] = (),
    ) -> None:
        """Store a row for every (item, arm) not in `done`. `previous` rows seed the answer cache."""
        self.check_arms(arm_names)
        for row in previous:
            if row.error is None:
                self._answers.seed(
                    (row.item_id, row.model_used),
                    Produced(row.answer, row.model_used, row.cost_usd or 0.0, row.latency_ms or 0.0),
                )
        gate = asyncio.Semaphore(max(self._cfg.concurrency, 1))

        async def one(item: GoldenItem, arm: str) -> None:
            async with gate:
                result = await self._row(run_id, item, arm)
            self._repo.insert_eval_result(result)
            if self._progress is not None:
                self._progress(result)

        await asyncio.gather(
            *(one(item, arm) for item in items for arm in arm_names if (item.id, arm) not in done)
        )

    async def _predict(self, item: GoldenItem) -> Prediction:
        router = self._cfg.router
        assert router is not None
        return await self._predictions.get(item.id, lambda: router.predict(item))

    async def _produce(self, item: GoldenItem, provider: EvalProvider, model: str) -> Produced:
        async def make() -> Produced:
            reply = await provider.answer(render_prompt(item), model)
            return Produced(reply.text, model, reply.cost_usd, reply.latency_ms)

        return await self._answers.get((item.id, model), make)

    async def _judgment(self, item: GoldenItem, model: str, answer: str) -> Judgment:
        provider, judge_model = self._cfg.judge
        return await self._judgments.get(
            (item.id, model), lambda: judge_answer(provider, judge_model, item, answer)
        )

    async def _row(self, run_id: int, item: GoldenItem, arm: str) -> EvalResult:
        predicted: str | None = None
        prediction_error: str | None = None
        if self._cfg.router is not None:
            try:
                predicted = (await self._predict(item)).tier
            except (EngineError, EvalError) as exc:
                prediction_error = f"router: {exc}"

        def row(model: str, **fields: Any) -> EvalResult:
            return EvalResult(
                run_id=run_id,
                item_id=item.id,
                arm=arm,
                model_used=model,
                expected_tier=item.expected_tier,
                predicted_tier=predicted,
                **fields,
            )

        if arm == ROUTER_ARM:
            if predicted is None:
                return row("", answer="", error=prediction_error)
            model = self._cfg.tier_models[predicted]
            provider = self._provider_for(model)
        elif arm == ORACLE_ARM:
            model = self._cfg.tier_models[item.expected_tier]
            provider = self._provider_for(model)
        else:
            provider, model = self._cfg.arms[arm]

        try:
            produced = await self._produce(item, provider, model)
        except EvalError as exc:
            return row(model, answer="", error=f"answer: {exc}")
        shared = {"answer": produced.text, "cost_usd": produced.cost_usd, "latency_ms": produced.latency_ms}

        try:
            judgment = await self._judgment(item, model, produced.text)
        except EvalError as exc:
            return row(model, error=f"judge: {exc}", **shared)
        rubric: dict[str, Any] = {"judge": judgment.to_json()}
        rejudge = self._cfg.rejudge
        if rejudge is not None and in_rejudge_sample(item.id, arm, self._cfg.rejudge_fraction):
            try:
                second = await judge_answer(rejudge[0], rejudge[1], item, produced.text)
                rubric["rejudge"] = second.to_json()
            except EvalError as exc:
                rubric["rejudge"] = {"model": rejudge[1], "error": str(exc)}
        return row(model, judge_score=judgment.score, rubric_hits=rubric, **shared)

    def _provider_for(self, model: str) -> EvalProvider:
        for provider, arm_model in self._cfg.arms.values():
            if arm_model == model:
                return provider
        return self._cfg.tier_provider


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def run_eval(
    runner: EvalRunner,
    repo: Repo,
    items: Sequence[GoldenItem],
    arm_names: Sequence[str] | None,
    *,
    version: str,
    judge_model: str,
    resume: int | None = None,
) -> int:
    """Create (or resume) an eval run, fill in its rows, and return the run id.

    Resuming keeps rows that have no error, deletes the failed ones and redoes them. With
    `arm_names` None a resumed run reuses its own arms.
    """
    done: set[tuple[str, str]] = set()
    previous: list[EvalResult] = []
    if resume is None:
        if not arm_names:
            raise RunSetupError("no arms to run")
        runner.check_arms(arm_names)
        run_id = repo.insert_eval_run(
            EvalRun(dataset_version=version, arms=list(arm_names), judge_model=judge_model, started_at=_now())
        )
    else:
        run = repo.get_eval_run(resume)
        if run is None:
            raise RunSetupError(f"no eval run {resume}")
        if run.dataset_version != version:
            raise RunSetupError(
                f"run {resume} used dataset {run.dataset_version}, the file is now {version}; start a new run"
            )
        if arm_names is not None and set(arm_names) != set(run.arms):
            raise RunSetupError(f"run {resume} has arms {', '.join(run.arms)}; resume it with those")
        arm_names = run.arms
        repo.delete_eval_errors(resume)
        previous = repo.list_eval_results(resume)
        done = {(r.item_id, r.arm) for r in previous}
        run_id = resume
    await runner.run(run_id, items, arm_names, done, previous)
    repo.finish_eval_run(run_id, _now())
    return run_id
