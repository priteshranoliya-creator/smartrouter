import json
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from smartrouter import cli
from smartrouter.decision.engine import DecisionState, FakeEngine
from smartrouter.eval.dataset import GoldenItem, dataset_version
from smartrouter.eval.router_arm import RouterArm
from smartrouter.eval.runner import (
    EvalRunner,
    RunnerConfig,
    RunSetupError,
    run_eval,
)
from smartrouter.policy.rules import PolicyContext
from smartrouter.settings import ROOT, Settings, load_settings
from smartrouter.telemetry.models import EvalResult
from smartrouter.telemetry.pricing import load_pricing
from smartrouter.telemetry.repo import Repo
from tests.unit.eval_fakes import FakeProvider, all_hit, item

OPUS, SONNET, HAIKU = "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"
ITEMS = [
    item(1, "haiku", "easy"),
    item(2, "sonnet", "mid"),
    item(3, "opus", "hard"),
    item(4, "sonnet", "unsure"),
]
TIER_MODELS = {"haiku": HAIKU, "sonnet": SONNET, "opus": OPUS}
LABELS = {
    "easy": {"A": 0.95, "B": 0.03, "C": 0.01, "D": 0.01},
    "mid": {"A": 0.05, "B": 0.9, "C": 0.03, "D": 0.02},
    "hard": {"A": 0.0, "B": 0.05, "C": 0.05, "D": 0.9},
    "unsure": {"A": 0.3, "B": 0.25, "C": 0.25, "D": 0.2},
}
SETTINGS = load_settings(env={})


def respond(state: DecisionState) -> dict[str, dict[str, float]]:
    marker = state.prompt.split()[0]
    return {"complexity": LABELS[marker]}


def make_context() -> PolicyContext:
    router = SETTINGS.router
    return PolicyContext(router.policy, router.tiers, load_pricing(ROOT / "config/pricing.yaml"))


def make_router() -> tuple[RouterArm, FakeEngine]:
    engine = FakeEngine(SETTINGS.questions, respond)
    return RouterArm(engine, SETTINGS, make_context()), engine


def make_config(
    provider: FakeProvider,
    *,
    arms: list[str] | None = None,
    judge: FakeProvider | None = None,
    tier_provider: FakeProvider | None = None,
    rejudge: FakeProvider | None = None,
    fraction: float = 0.0,
    router: RouterArm | None = None,
    concurrency: int = 4,
) -> RunnerConfig:
    fixed = {"opus": (provider, OPUS), "sonnet": (provider, SONNET), "haiku": (provider, HAIKU)}
    return RunnerConfig(
        arms={name: fixed[name] for name in (arms if arms is not None else fixed)},
        tier_models=TIER_MODELS,
        tier_provider=tier_provider or FakeProvider(),
        judge=(judge or FakeProvider(), "judge-model"),
        rejudge=None if rejudge is None else (rejudge, "rejudge-model"),
        rejudge_fraction=fraction,
        router=router,
        concurrency=concurrency,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[Repo]:
    with Repo.open(tmp_path / "r.db") as r:
        yield r


async def run(
    repo: Repo,
    config: RunnerConfig,
    arms: list[str] | None,
    items: list[GoldenItem] = ITEMS,
    resume: int | None = None,
) -> int:
    return await run_eval(
        EvalRunner(repo, config),
        repo,
        items,
        arms,
        version="v1",
        judge_model="judge-model",
        resume=resume,
    )


FIVE = ["opus", "sonnet", "haiku", "router", "oracle"]


async def test_one_row_per_item_and_arm_with_every_field(repo: Repo) -> None:
    router, _ = make_router()
    run_id = await run(repo, make_config(FakeProvider(), router=router), FIVE)

    rows = repo.list_eval_results(run_id)
    assert len(rows) == 20
    assert Counter((r.item_id, r.arm) for r in rows) == {(i.id, a): 1 for i in ITEMS for a in FIVE}
    by = {(r.item_id, r.arm): r for r in rows}
    opus_row = by[("g-0001", "opus")]
    assert opus_row.model_used == OPUS
    assert opus_row.answer == f"answer:{OPUS}"
    assert opus_row.judge_score == 1.0
    assert opus_row.rubric_hits is not None
    assert opus_row.rubric_hits["judge"]["pass"] is True
    assert "rejudge" not in opus_row.rubric_hits
    assert (opus_row.cost_usd, opus_row.latency_ms) == (0.03, 100.0)
    assert opus_row.error is None
    for r in rows:
        assert r.expected_tier == next(i.expected_tier for i in ITEMS if i.id == r.item_id)
    assert {r.predicted_tier for r in rows if r.item_id == "g-0001"} == {"haiku"}
    run_row = repo.get_eval_run(run_id)
    assert run_row is not None
    assert (run_row.arms, run_row.judge_model, run_row.dataset_version) == (FIVE, "judge-model", "v1")
    assert run_row.finished_at is not None


async def test_each_item_and_model_is_answered_once_and_copied_to_virtual_arms(repo: Repo) -> None:
    provider = FakeProvider()
    router, _ = make_router()
    run_id = await run(repo, make_config(provider, router=router), FIVE)

    assert set(provider.answer_calls.values()) == {1}
    assert sum(provider.answer_calls.values()) == 12  # 4 items x 3 models; router and oracle add none
    by = {(r.item_id, r.arm): r for r in repo.list_eval_results(run_id)}
    for item_id in ("g-0001", "g-0003"):
        oracle, router_row = by[(item_id, "oracle")], by[(item_id, "router")]
        same = by[(item_id, "haiku" if item_id == "g-0001" else "opus")]
        for copy in (oracle, router_row):
            assert (copy.model_used, copy.answer, copy.cost_usd, copy.latency_ms) == (
                same.model_used,
                same.answer,
                same.cost_usd,
                same.latency_ms,
            )


async def test_models_no_fixed_arm_covers_are_produced_once_by_the_tier_provider(repo: Repo) -> None:
    fixed, tier = FakeProvider(), FakeProvider()
    router, _ = make_router()
    config = make_config(fixed, arms=["haiku"], tier_provider=tier, router=router)
    run_id = await run(repo, config, ["haiku", "router", "oracle"])

    assert sum(fixed.answer_calls.values()) == 4  # haiku arm, 4 items
    # router and oracle want sonnet for g-0002 and g-0004, opus for g-0003; haiku is the fixed arm's
    assert set(tier.answer_calls.values()) == {1}
    assert {model for _, model in tier.answer_calls} == {SONNET, OPUS}
    assert sum(tier.answer_calls.values()) == 3
    assert len(repo.list_eval_results(run_id)) == 12


async def test_router_serves_the_policy_tier_and_the_fallback_when_unsure(repo: Repo) -> None:
    router, engine = make_router()
    run_id = await run(repo, make_config(FakeProvider(), router=router), ["router"])
    by = {r.item_id: r for r in repo.list_eval_results(run_id)}
    assert {k: (v.model_used, v.predicted_tier) for k, v in by.items()} == {
        "g-0001": (HAIKU, "haiku"),
        "g-0002": (SONNET, "sonnet"),
        "g-0003": (OPUS, "opus"),
        "g-0004": (SONNET, "sonnet"),  # low confidence: policy abstains, fallback tier serves
    }
    assert len(engine.calls) == 4  # one engine call per item, however many rows reuse it
    assert all("Session facts" not in c.render() for c in engine.calls)


async def test_router_fallback_tier_comes_from_settings() -> None:
    config = SETTINGS.router.eval.model_copy(update={"router_fallback_tier": "opus"})
    settings = Settings(
        router=SETTINGS.router.model_copy(update={"eval": config}),
        questions=SETTINGS.questions,
        secrets=SETTINGS.secrets,
    )
    arm = RouterArm(FakeEngine(SETTINGS.questions, respond), settings, make_context())
    prediction = await arm.predict(ITEMS[3])
    assert (prediction.tier, prediction.desired_tier, prediction.rule, prediction.fell_back) == (
        "opus",
        "sonnet",
        "low_confidence",
        True,
    )


async def test_oracle_uses_the_expected_tier(repo: Repo) -> None:
    run_id = await run(repo, make_config(FakeProvider()), ["oracle"])
    by = {r.item_id: r for r in repo.list_eval_results(run_id)}
    assert {k: v.model_used for k, v in by.items()} == {
        "g-0001": HAIKU,
        "g-0002": SONNET,
        "g-0003": OPUS,
        "g-0004": SONNET,
    }
    assert all(r.predicted_tier is None for r in by.values())  # no router in this run


async def test_context_is_part_of_the_router_input() -> None:
    engine = FakeEngine(SETTINGS.questions)
    router = RouterArm(engine, SETTINGS, make_context())
    with_context = item(9, "haiku", "easy").model_copy(update={"context": "def f(): pass"})
    await router.predict(with_context)
    assert engine.calls[0].prompt == "def f(): pass\n\neasy request"


async def test_answer_error_makes_an_error_row_and_skips_the_judge(repo: Repo) -> None:
    provider = FakeProvider(fail_models={OPUS})
    judge = FakeProvider()
    run_id = await run(repo, make_config(provider, judge=judge), ["opus", "sonnet"])
    by = {(r.item_id, r.arm): r for r in repo.list_eval_results(run_id)}
    bad = by[("g-0001", "opus")]
    assert (bad.answer, bad.judge_score, bad.rubric_hits, bad.model_used) == ("", None, None, OPUS)
    assert bad.error == f"answer: {OPUS} is down"
    assert by[("g-0001", "sonnet")].error is None
    assert sum(judge.judge_calls.values()) == 4  # only the four sonnet answers


async def test_unusable_judge_json_stores_the_answer_with_an_error(repo: Repo) -> None:
    judge = FakeProvider(judge_reply=lambda prompt, model: "garbage")
    run_id = await run(repo, make_config(FakeProvider(), judge=judge), ["haiku"], ITEMS[:1])
    (only,) = repo.list_eval_results(run_id)
    assert only.answer == f"answer:{HAIKU}"
    assert only.judge_score is None
    assert only.error is not None and only.error.startswith("judge: ")
    assert only.cost_usd == 0.002


async def test_judge_runs_once_per_item_and_model_shared_by_virtual_arms(repo: Repo) -> None:
    judge = FakeProvider()
    router, _ = make_router()
    await run(repo, make_config(FakeProvider(), judge=judge, router=router), FIVE)
    assert sum(judge.judge_calls.values()) == 12


async def test_rejudge_is_stored_for_the_sample_and_failures_do_not_fail_the_row(repo: Repo) -> None:
    every = await run(
        repo,
        make_config(FakeProvider(), rejudge=FakeProvider(), fraction=1.0),
        ["opus"],
        ITEMS[:2],
    )
    for r in repo.list_eval_results(every):
        assert r.rubric_hits is not None
        assert r.rubric_hits["rejudge"]["model"] == "rejudge-model"
        assert r.rubric_hits["rejudge"]["pass"] is True

    broken = FakeProvider(judge_reply=lambda prompt, model: "garbage")
    failed = await run(repo, make_config(FakeProvider(), rejudge=broken, fraction=1.0), ["opus"], ITEMS[:1])
    (row,) = repo.list_eval_results(failed)
    assert row.error is None and row.judge_score == 1.0
    assert row.rubric_hits is not None
    assert row.rubric_hits["rejudge"]["model"] == "rejudge-model"
    assert "invalid judge JSON" in row.rubric_hits["rejudge"]["error"]

    none = await run(repo, make_config(FakeProvider(), rejudge=FakeProvider(), fraction=0.0), ["opus"])
    assert all("rejudge" not in (r.rubric_hits or {}) for r in repo.list_eval_results(none))


async def test_rejudge_sample_tracks_the_fraction_over_many_rows(repo: Repo) -> None:
    many = [item(n, "haiku", "easy") for n in range(1, 301)]
    run_id = await run(
        repo, make_config(FakeProvider(), rejudge=FakeProvider(), fraction=0.1), ["haiku"], many
    )
    rows = repo.list_eval_results(run_id)
    sampled = {r.item_id for r in rows if "rejudge" in (r.rubric_hits or {})}
    assert 0.05 <= len(sampled) / 300 <= 0.15
    again = await run(
        repo, make_config(FakeProvider(), rejudge=FakeProvider(), fraction=0.1), ["haiku"], many
    )
    assert {r.item_id for r in repo.list_eval_results(again) if "rejudge" in (r.rubric_hits or {})} == sampled


async def test_resume_skips_finished_rows_and_redoes_errors(repo: Repo) -> None:
    flaky = FakeProvider(fail_models={SONNET})
    run_id = await run(repo, make_config(flaky), ["opus", "sonnet"])
    first = repo.list_eval_results(run_id)
    assert sum(1 for r in first if r.error) == 4

    healthy = FakeProvider()
    await run(repo, make_config(healthy), None, resume=run_id)

    rows = repo.list_eval_results(run_id)
    assert len(rows) == 8
    assert all(r.error is None for r in rows)
    assert {m for _, m in healthy.answer_calls} == {SONNET}  # opus rows were not redone
    assert sum(healthy.answer_calls.values()) == 4


async def test_resume_reuses_finished_answers_for_virtual_arms(repo: Repo) -> None:
    first = FakeProvider()
    run_id = await run(repo, make_config(first), ["haiku", "sonnet", "opus"])
    # the run's own arms grow no new rows; a later resume with nothing to do makes no calls
    again = FakeProvider()
    await run(repo, make_config(again), None, resume=run_id)
    assert sum(again.answer_calls.values()) == 0
    assert len(repo.list_eval_results(run_id)) == 12


async def test_resume_rejects_unknown_run_changed_dataset_and_other_arms(repo: Repo) -> None:
    run_id = await run(repo, make_config(FakeProvider()), ["opus"])
    config = make_config(FakeProvider())
    with pytest.raises(RunSetupError, match="no eval run 99"):
        await run(repo, config, None, resume=99)
    with pytest.raises(RunSetupError, match="start a new run"):
        await run_eval(
            EvalRunner(repo, config), repo, ITEMS, None, version="other", judge_model="j", resume=run_id
        )
    with pytest.raises(RunSetupError, match="has arms opus"):
        await run(repo, config, ["sonnet"], resume=run_id)


async def test_unknown_arm_and_router_without_engine_are_rejected(repo: Repo) -> None:
    config = make_config(FakeProvider())
    with pytest.raises(RunSetupError, match=r"unknown arm\(s\) nope"):
        await run(repo, config, ["nope"])
    with pytest.raises(RunSetupError, match="needs a decision engine"):
        await run(repo, config, ["router"])
    with pytest.raises(RunSetupError, match="no arms"):
        await run(repo, config, [])


async def test_progress_reports_each_finished_row(repo: Repo) -> None:
    seen: list[EvalResult] = []
    runner = EvalRunner(repo, make_config(FakeProvider()), seen.append)
    await run_eval(runner, repo, ITEMS[:2], ["opus", "oracle"], version="v", judge_model="j")
    assert len(seen) == 4


async def test_concurrency_limit_is_respected(repo: Repo) -> None:
    import asyncio

    active = 0
    peak = 0

    class Slow(FakeProvider):
        async def answer(self, prompt, model, *, system=None, json_mode=False):
            nonlocal active, peak
            if system is None:
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.01)
                active -= 1
            return await super().answer(prompt, model, system=system, json_mode=json_mode)

    many = [item(n, "haiku", "easy") for n in range(1, 11)]
    await run(repo, make_config(Slow(), concurrency=2), ["opus"], many)
    assert peak == 2


def test_dataset_version_is_twelve_hex_of_the_file_hash(tmp_path: Path) -> None:
    path = tmp_path / "g.jsonl"
    path.write_text("abc\n")
    import hashlib

    assert dataset_version(path) == hashlib.sha256(b"abc\n").hexdigest()[:12]
    assert len(dataset_version(path)) == 12


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "golden_small.jsonl"
cli_runner = CliRunner()


def test_cli_run_stores_rows_and_prints_run_id_and_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "r.db"
    router = SETTINGS.router.model_copy(update={"paths": SETTINGS.router.paths.model_copy(update={"db": db})})
    settings = Settings(router=router, questions=SETTINGS.questions, secrets=SETTINGS.secrets)
    provider = FakeProvider(judge_reply=all_hit)
    fixed = {
        "opus": (provider, OPUS),
        "sonnet": (provider, SONNET),
        "gemini-argon": (provider, "gemini-4-argon"),
    }
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr("smartrouter.eval.providers.build_providers", lambda s, b: fixed)

    result = cli_runner.invoke(
        cli.app,
        ["eval", "run", "--dataset", str(FIXTURE), "--arms", "opus,oracle", "--limit", "2"],
    )
    assert result.exit_code == 0, result.output
    assert "run id: 1" in result.output
    lines = result.output.splitlines()
    summary = lines[lines.index("run id: 1") + 1 :]
    assert [ln.split()[0] for ln in summary[:3]] == ["arm", "opus", "oracle"]
    with Repo.open(db) as repo:
        rows = repo.list_eval_results(1)
        run_row = repo.get_eval_run(1)
    assert len(rows) == 4  # first 2 test items x 2 arms
    assert run_row is not None
    assert run_row.judge_model == "gemini-4-argon"
    assert run_row.dataset_version == dataset_version(FIXTURE)
    assert json.dumps(run_row.arms) == '["opus", "oracle"]'
    assert "g-0002 opus claude-opus-5-5 score 1.00" in result.output  # progress lines on stderr

    resumed = cli_runner.invoke(
        cli.app, ["eval", "run", "--dataset", str(FIXTURE), "--resume", "1", "--limit", "2"]
    )
    assert resumed.exit_code == 0, resumed.output
    assert "run id: 1" in resumed.output
