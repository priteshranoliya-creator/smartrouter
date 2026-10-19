"""Opt-in wiring of a project's `.claude/settings.local.json` to the router, and its exact reversal."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from smartrouter.settings import ROOT

DEFAULT_PORT = 8787
MANIFEST_DIR = ROOT / "data" / "installs"
BASE_URL_KEY = "ANTHROPIC_BASE_URL"

Json = dict[str, Any]


class InstallError(Exception):
    """A refusal the CLI reports as one line with exit code 1."""


@dataclass(frozen=True)
class Entries:
    env: dict[str, str]
    hooks: dict[str, str]


def _hook_command(script: Path) -> str:
    quoted = "'" + str(script).replace("'", "'\\''") + "'"
    return f"python3 {quoted} 2>/dev/null || true"


def router_entries(root: Path, port: int) -> Entries:
    env = {BASE_URL_KEY: f"http://127.0.0.1:{port}", "CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"}
    if port != DEFAULT_PORT:
        env["SMARTROUTER_PORT"] = str(port)
    hooks = {
        "UserPromptSubmit": _hook_command(root / "hooks" / "prompt_hook.py"),
        "PreCompact": _hook_command(root / "hooks" / "precompact_hook.py"),
    }
    return Entries(env=env, hooks=hooks)


def _group(command: str) -> Json:
    return {"hooks": [{"type": "command", "command": command}]}


def _is_ours(group: Any, command: str) -> bool:
    return bool(group == _group(command))


def apply(settings: Json, entries: Entries) -> Json:
    out = copy.deepcopy(settings)
    out.setdefault("env", {}).update(entries.env)
    hooks = out.setdefault("hooks", {})
    for event, command in entries.hooks.items():
        groups = hooks.setdefault(event, [])
        if not any(_is_ours(g, command) for g in groups):
            groups.append(_group(command))
    return out


def _had(original: Json | None, *path: str) -> bool:
    if original is None:
        return False
    node: Any = original
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return False
        node = node[key]
    return True


def remove(settings: Json, entries: Entries, original: Json | None = None) -> Json:
    """Drop our entries; prune containers we emptied unless `original` already held them."""
    out = copy.deepcopy(settings)
    env = out.get("env")
    if isinstance(env, dict):
        for key, value in entries.env.items():
            if env.get(key) == value:
                del env[key]
                if original is not None and key in original.get("env", {}):
                    env[key] = original["env"][key]
        if not env and not _had(original, "env"):
            del out["env"]
    hooks = out.get("hooks")
    if isinstance(hooks, dict):
        for event, command in entries.hooks.items():
            groups = hooks.get(event)
            if not isinstance(groups, list):
                continue
            groups[:] = [g for g in groups if not _is_ours(g, command)]
            if not groups and not _had(original, "hooks", event):
                del hooks[event]
        if not hooks and not _had(original, "hooks"):
            del out["hooks"]
    return out


def conflicts(settings: Json, entries: Entries) -> str | None:
    env = settings.get("env")
    current = env.get(BASE_URL_KEY) if isinstance(env, dict) else None
    if current is not None and current != entries.env[BASE_URL_KEY]:
        return f"{BASE_URL_KEY} is already {current!r}; rerun with --force to replace it"
    return None


def _settings_path(project: Path) -> Path:
    return project / ".claude" / "settings.local.json"


def _check_project(project: Path) -> None:
    if not project.is_dir():
        raise InstallError(f"{project} is not an existing directory")
    claude_dir = project / ".claude"
    if claude_dir.exists() and not claude_dir.is_dir():
        raise InstallError(f"{claude_dir} exists but is not a directory")


def manifest_path(project: Path, manifest_dir: Path) -> Path:
    digest = hashlib.sha256(str(project).encode()).hexdigest()[:16]
    return manifest_dir / f"{digest}.json"


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _parse(raw: bytes, path: Path) -> Json:
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise InstallError(f"{path} is not valid JSON ({exc}); nothing changed") from exc
    if not isinstance(parsed, dict):
        raise InstallError(f"{path} is not a JSON object; nothing changed")
    return parsed


def _dump(settings: Json) -> bytes:
    return (json.dumps(settings, indent=2, ensure_ascii=False) + "\n").encode()


def install(
    project: Path,
    root: Path,
    port: int,
    manifest_dir: Path = MANIFEST_DIR,
    force: bool = False,
) -> str:
    project = project.resolve()
    _check_project(project)
    path = _settings_path(project)
    entries = router_entries(root, port)
    existed = path.is_file()
    raw = path.read_bytes() if existed else b""
    current = _parse(raw, path) if existed else {}
    if not force and (problem := conflicts(current, entries)):
        raise InstallError(f"{problem}; nothing changed")
    updated = apply(current, entries)
    already = manifest_path(project, manifest_dir)
    if not already.exists():
        manifest = {
            "project": str(project),
            "existed": existed,
            "created_dir": not path.parent.exists(),
            "original_b64": base64.b64encode(raw).decode(),
            "entries": {"env": entries.env, "hooks": entries.hooks},
        }
        _atomic_write(already, json.dumps(manifest, indent=2).encode())
    if updated == current and existed:
        return f"already installed in {path}"
    _atomic_write(path, _dump(updated))
    return f"installed router entries in {path}"


def uninstall(project: Path, manifest_dir: Path = MANIFEST_DIR) -> str:
    project = project.resolve()
    path = _settings_path(project)
    mpath = manifest_path(project, manifest_dir)
    if not mpath.exists():
        return f"nothing to uninstall for {project} (no install record)"
    manifest: Mapping[str, Any] = json.loads(mpath.read_text())
    entries = Entries(env=manifest["entries"]["env"], hooks=manifest["entries"]["hooks"])
    existed: bool = manifest["existed"]
    original_raw = base64.b64decode(manifest["original_b64"])
    original = json.loads(original_raw) if existed else {}
    if not path.is_file():
        mpath.unlink()
        return f"{path} is gone; dropped install record"
    result = remove(_parse(path.read_bytes(), path), entries, original)
    if result == original:
        if existed:
            _atomic_write(path, original_raw)
        else:
            path.unlink()
            if manifest["created_dir"] and not any(path.parent.iterdir()):
                path.parent.rmdir()
        message = f"removed router entries from {path}; restored original"
    else:
        _atomic_write(path, _dump(result))
        message = f"removed router entries from {path}; kept your other edits"
    mpath.unlink()
    return message
