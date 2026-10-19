"""`router` command line: serve the proxy, wire or unwire a project."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from smartrouter import install as installer
from smartrouter.eval.dataset import DatasetError, GoldenItem, Report, load_dataset, validate
from smartrouter.settings import ROOT, load_settings
from smartrouter.telemetry import stats
from smartrouter.telemetry.export import write_csv
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
