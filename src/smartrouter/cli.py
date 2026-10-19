"""`router` command line: serve the proxy, wire or unwire a project."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from smartrouter import install as installer
from smartrouter.settings import ROOT, load_settings
from smartrouter.telemetry import stats
from smartrouter.telemetry.export import write_csv
from smartrouter.telemetry.repo import Repo

app = typer.Typer(help="Local cache-aware model router for Claude Code.", no_args_is_help=True)

ProjectOption = Annotated[Path, typer.Option("--project", help="Project directory to wire up.")]
ManifestOption = Annotated[Path, typer.Option("--manifest-dir", hidden=True)]


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
