"""T6.3: run the router as a per-user launchd agent, started at login and restarted if it exits."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from smartrouter.settings import ROOT

LABEL = "dev.smartrouter.router"
AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
LOG_DIR = ROOT / "data" / "logs"
THROTTLE_S = 10  # launchd waits this long between restarts, e.g. while the port is still taken

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class ServiceError(Exception):
    """A refusal the CLI reports as one line with exit code 1."""


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


def render_plist(root: Path, log_dir: Path) -> bytes:
    """The agent: `.venv/bin/router serve` from the repo, so `.env` and `config/` resolve as in a shell."""
    return plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": [str(root / ".venv" / "bin" / "router"), "serve"],
            "WorkingDirectory": str(root),
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": THROTTLE_S,
            "ProcessType": "Background",
            "StandardOutPath": str(log_dir / "router.out.log"),
            "StandardErrorPath": str(log_dir / "router.err.log"),
        }
    )


@dataclass(frozen=True)
class Service:
    root: Path = ROOT
    agents_dir: Path = AGENTS_DIR
    log_dir: Path = LOG_DIR
    run: Runner = _run
    platform: str = sys.platform
    uid: int = os.getuid()

    @property
    def plist(self) -> Path:
        return self.agents_dir / f"{LABEL}.plist"

    @property
    def target(self) -> str:
        return f"gui/{self.uid}/{LABEL}"

    def _require_macos(self) -> None:
        if self.platform != "darwin":
            raise ServiceError(f"launchd service is macOS only (this is {self.platform})")

    def install(self) -> str:
        self._require_macos()
        program = self.root / ".venv" / "bin" / "router"
        if not program.exists():
            raise ServiceError(f"{program} not found; run `uv sync` first")
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=self.agents_dir, delete=False, suffix=".tmp") as tmp:
            staged = Path(tmp.name)
            try:
                tmp.write(render_plist(self.root, self.log_dir))
            except BaseException:
                staged.unlink(missing_ok=True)
                raise
        staged.replace(self.plist)
        self.run(["launchctl", "bootout", self.target])  # reload an older copy; fails harmlessly when absent
        result = self.run(["launchctl", "bootstrap", f"gui/{self.uid}", str(self.plist)])
        if result.returncode != 0:
            self.plist.unlink(missing_ok=True)  # else launchd would retry it at every login
            raise ServiceError(f"launchctl bootstrap failed: {(result.stderr or result.stdout).strip()}")
        return f"installed {self.plist}; logs in {self.log_dir}"

    def uninstall(self) -> str:
        self._require_macos()
        loaded = self.run(["launchctl", "bootout", self.target]).returncode == 0
        existed = self.plist.exists()
        self.plist.unlink(missing_ok=True)
        if not (loaded or existed):
            return "not installed"
        return f"removed {self.plist}"

    def loaded(self) -> bool:
        self._require_macos()
        return self.run(["launchctl", "print", self.target]).returncode == 0
