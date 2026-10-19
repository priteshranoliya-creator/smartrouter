"""`router` command line: serve the proxy, wire or unwire a project."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from smartrouter import install as installer
from smartrouter.settings import ROOT, load_settings

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
