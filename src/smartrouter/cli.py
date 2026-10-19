"""`router` command line: serve the proxy, wire or unwire a project."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import httpx
import typer
import uvicorn

from smartrouter import install as installer
from smartrouter.decision.calibrated import write_calibration
from smartrouter.decision.engine import EngineError
from smartrouter.decision.factory import build_engine, build_raw_engine
from smartrouter.eval.calibrate import CalibrationRun
from smartrouter.eval.dataset import DatasetError, GoldenItem, Report, load_dataset, validate
from smartrouter.eval.router_arm import RouterArm
from smartrouter.policy.rules import PolicyContext
from smartrouter.settings import ROOT, load_settings
from smartrouter.telemetry import stats
from smartrouter.telemetry.export import write_csv
from smartrouter.telemetry.models import EvalResult
from smartrouter.telemetry.pricing import PricingError, load_pricing
from smartrouter.telemetry.repo import Repo

app = typer.Typer(help="Local cache-aware model router for Claude Code.", no_args_is_help=True)

ProjectOption = Annotated[Path, typer.Option("--project", help="Project directory to wire up.")]
ManifestOption = Annotated[Path, typer.Option("--manifest-dir", hidden=True)]
DatasetOption = Annotated[Path, typer.Option("--dataset", help="Golden dataset (JSONL).")]
DEFAULT_DATASET = ROOT / "data/golden/golden.jsonl"

eval_app = typer.Typer(help="Golden dataset and eval runs.", no_args_is_help=True)
app.add_typer(eval_app, name="eval")


@app.command()
def serve() -> None:
    """Run the proxy on the configured loopback host and port."""
    server = load_settings().router.server
    uvicorn.run("smartrouter.app:create_app", factory=True, host=server.host, port=server.port)


@app.command()
def install(
    project: ProjectOption,
    force: Annotated[bool, typer.Option("--force", help="Replace a foreign ANTHROPIC_BASE_URL.")] = False,
    manifest_dir: ManifestOption = installer.MANIFEST_DIR,
) -> None:
    """Add the router's env and hooks to PROJECT/.claude/settings.local.json."""
    port = load_settings().router.server.port
    try:
        typer.echo(installer.install(project, ROOT, port, manifest_dir, force))
    except installer.InstallError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command()
def uninstall(project: ProjectOption, manifest_dir: ManifestOption = installer.MANIFEST_DIR) -> None:
    """Remove the router's entries, restoring the original file when untouched."""
    try:
        typer.echo(installer.uninstall(project, manifest_dir))
    except installer.InstallError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command()
def export(
    since: Annotated[str, typer.Option("--since", help=f"Window: {', '.join(stats.WINDOWS)}.")] = "all",
    out: Annotated[Path | None, typer.Option("--out", help="Write here instead of stdout.")] = None,
) -> None:
    """Write one CSV row per request in the window, read straight from the database."""
    try:
        lower_bound = stats.window_since(since, datetime.now(UTC))
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    db = load_settings().path("db")
    if not db.exists():
        typer.echo(f"error: no database at {db}", err=True)
        raise typer.Exit(1)
    with Repo.open(db) as repo:
        rows = repo.request_rows(lower_bound)
    if out is None:
        write_csv(rows, sys.stdout)
    else:
        with out.open("w", newline="") as handle:
            write_csv(rows, handle)


def _load_items(dataset: Path, tiers: list[str]) -> list[GoldenItem]:
    try:
        return load_dataset(dataset, tiers)
    except DatasetError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


def _format_report(report: Report) -> str:
    splits = ("train", "test")
    width = max([len(t) for t in report.tiers] + [len("tier")])
    lines = [f"{'tier':<{width}}  train  test"]
    for tier in report.tiers:
        counts = [report.tier_split.get((tier, split), 0) for split in splits]
        lines.append(f"{tier:<{width}}  {counts[0]:>5}  {counts[1]:>4}")
    lines.append("")
    lines.append(f"items: {report.total}  drafts: {report.drafts}")
    lines.append("task types: " + ", ".join(f"{t}={n}" for t, n in report.task_types.items()))
    return "\n".join(lines)


@eval_app.command("validate")
def eval_validate(dataset: DatasetOption = DEFAULT_DATASET) -> None:
    """Check the golden dataset: schema, ids, tiers, the train/test split and tier balance."""
    tiers = list(load_settings().router.tiers)
    report = validate(_load_items(dataset, tiers), tiers)
    typer.echo(_format_report(report))
    for warning in report.warnings:
        typer.echo(f"warning: {warning}")
    for problem in report.problems:
        typer.echo(f"error: {problem}", err=True)
    if not report.ok:
        raise typer.Exit(1)
    typer.echo("ok")


@eval_app.command("smoke")
def eval_smoke(
    item: Annotated[str, typer.Option("--item", help="Golden item id, e.g. g-0001.")],
    arms: Annotated[
        str | None, typer.Option("--arms", help="Comma-separated arm names (default: all).")
    ] = None,
    dataset: DatasetOption = DEFAULT_DATASET,
) -> None:
    """Answer one golden item with each arm and print tokens, cost, latency and the answer's start."""
    from smartrouter.eval.providers import EvalError, build_providers, render_prompt

    settings = load_settings()
    items = {i.id: i for i in _load_items(dataset, list(settings.router.tiers))}
    if item not in items:
        typer.echo(f"error: no item {item!r} in {dataset}", err=True)
        raise typer.Exit(2)
    try:
        book = load_pricing(ROOT / "config/pricing.yaml")
        available = build_providers(settings, book)
    except (PricingError, EvalError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    names = [a.strip() for a in arms.split(",") if a.strip()] if arms else list(available)
    if unknown := [a for a in names if a not in available]:
        typer.echo(f"error: unknown arm(s) {', '.join(unknown)} (known: {', '.join(available)})", err=True)
        raise typer.Exit(2)
    prompt = render_prompt(items[item])

    async def run() -> int:
        failed = 0
        for arm in names:
            provider, model = available[arm]
            try:
                answer = await provider.answer(prompt, model)
            except EvalError as exc:
                failed += 1
                typer.echo(f"{arm}\t{model}\terror: {exc}")
                continue
            usage = answer.usage
            text = " ".join(answer.text.split())[:120]
            typer.echo(
                f"{arm}\t{model}\tin={usage.input_tokens} cache_read={usage.cache_read_tokens} "
                f"cache_write={usage.cache_write_tokens} out={usage.output_tokens}\t"
                f"${answer.cost_usd:.6f}\t{answer.latency_ms:.0f}ms\t{text}"
            )
        return failed

    if asyncio.run(run()):
        raise typer.Exit(1)


@eval_app.command("run")
def eval_run(
    dataset: DatasetOption = DEFAULT_DATASET,
    split: Annotated[str, typer.Option("--split", help="train or test.")] = "test",
    arms: Annotated[
        str | None,
        typer.Option("--arms", help="Comma-separated arms (default: every configured arm, router, oracle)."),
    ] = None,
    judge: Annotated[str | None, typer.Option("--judge", help="Judge arm (default: eval.judge).")] = None,
    limit: Annotated[int | None, typer.Option("--limit", min=1, help="Only the first N items.")] = None,
    concurrency: Annotated[int, typer.Option("--concurrency", min=1, help="Parallel rows.")] = 4,
    resume: Annotated[int | None, typer.Option("--resume", help="Finish this run id.")] = None,
) -> None:
    """Answer and judge every item of a split with every arm, store the rows, print a summary."""
    from smartrouter.eval.dataset import dataset_version
    from smartrouter.eval.providers import ClaudeCliProvider, EvalError, build_providers
    from smartrouter.eval.report import format_summary, summarize
    from smartrouter.eval.runner import VIRTUAL_ARMS, EvalRunner, RunnerConfig, RunSetupError, run_eval

    def fail(message: str, code: int = 1) -> typer.Exit:
        typer.echo(f"error: {message}", err=True)
        return typer.Exit(code)

    settings = load_settings()
    router = settings.router
    if split not in ("train", "test"):
        raise fail(f"--split must be train or test, got {split!r}", 2)
    try:
        items = [i for i in load_dataset(dataset, list(router.tiers)) if i.split == split][:limit]
        version = dataset_version(dataset)
        book = load_pricing(ROOT / "config/pricing.yaml")
        available = build_providers(settings, book)
    except DatasetError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    except (PricingError, EvalError) as exc:
        raise fail(str(exc)) from exc
    if not items:
        raise fail(f"no {split} items in {dataset}")
    judge_name = judge or router.eval.judge
    if judge_name not in available:
        raise fail(f"unknown judge arm {judge_name!r} (known: {', '.join(available)})", 2)

    with Repo.open(settings.path("db")) as repo:
        names = [a.strip() for a in arms.split(",") if a.strip()] if arms else None
        if names is None:
            if resume is None:
                names = [*available, *VIRTUAL_ARMS]
            elif (previous := repo.get_eval_run(resume)) is not None:
                names = previous.arms
            else:
                raise fail(f"no eval run {resume}", 2)
        names = list(dict.fromkeys(names))

        def progress(result: EvalResult) -> None:
            outcome = f"error: {result.error}" if result.error else f"score {result.judge_score:.2f}"
            typer.echo(f"{result.item_id} {result.arm} {result.model_used} {outcome}", err=True)

        async def run() -> int:
            async with httpx.AsyncClient() as http:
                router_arm = None
                if "router" in names:
                    context = PolicyContext(router.policy, router.tiers, book)
                    router_arm = RouterArm(build_engine(settings, http), settings, context)
                config = RunnerConfig(
                    arms=available,
                    tier_models={name: tier.model for name, tier in router.tiers.items()},
                    tier_provider=ClaudeCliProvider(book),
                    judge=available[judge_name],
                    rejudge=available[router.eval.rejudge_arm],
                    rejudge_fraction=router.eval.rejudge_fraction,
                    router=router_arm,
                    concurrency=concurrency,
                )
                return await run_eval(
                    EvalRunner(repo, config, progress),
                    repo,
                    items,
                    names,
                    version=version,
                    judge_model=available[judge_name][1],
                    resume=resume,
                )

        try:
            run_id = asyncio.run(run())
        except RunSetupError as exc:
            raise fail(str(exc), 2) from exc
        except EngineError as exc:
            raise fail(str(exc)) from exc
        typer.echo(f"run id: {run_id}")
        typer.echo(format_summary(summarize(repo.list_eval_results(run_id))))


@eval_app.command("calibrate")
def eval_calibrate(
    dataset: DatasetOption = DEFAULT_DATASET,
    split: Annotated[
        str, typer.Option("--split", help="Split to fit on; the other split scores it.")
    ] = "train",
    concurrency: Annotated[int, typer.Option("--concurrency", min=1, help="Parallel engine calls.")] = 4,
) -> None:
    """Fit the routing question's temperature on the train split and score it on the test split."""
    from smartrouter.eval.calibrate import CalibrationRunError, render_report, run_calibration
    from smartrouter.eval.dataset import dataset_version

    settings = load_settings()
    router = settings.router
    if split not in ("train", "test"):
        typer.echo(f"error: --split must be train or test, got {split!r}", err=True)
        raise typer.Exit(2)
    items = _load_items(dataset, list(router.tiers))
    version = dataset_version(dataset)
    now = datetime.now(UTC)
    cache = settings.path("calibration").with_name("calibration_raw.jsonl")

    async def run() -> CalibrationRun:
        async with httpx.AsyncClient() as http:
            return await run_calibration(
                build_raw_engine(settings, http),
                items,
                question=router.decision.question,
                tier_map=router.decision.tier_map,
                tiers=list(router.tiers),
                version=version,
                cache=cache,
                fit_split=split,
                concurrency=concurrency,
                now=now,
            )

    try:
        result = asyncio.run(run())
    except (EngineError, CalibrationRunError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    write_calibration(settings.path("calibration"), result.calibration)
    report = render_report(result, str(dataset))
    reports = settings.path("reports")
    reports.mkdir(parents=True, exist_ok=True)
    (reports / f"calibration-{now:%Y%m%d-%H%M}.md").write_text(report, encoding="utf-8")
    typer.echo(report)
