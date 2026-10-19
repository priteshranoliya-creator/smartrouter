import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from smartrouter import install as installer
from smartrouter.cli import app
from smartrouter.install import Entries, apply, remove, router_entries

runner = CliRunner()

MESSY = (
    b'{\n\t"zeta": "\xc3\xa9\xe2\x9c\x93",\n\t"env": {"MINE": "1", "A": "b"},\n'
    b'\t"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "echo hi"}]}]},\n'
    b'\t"alpha": [3,1,2]}'
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "proj"
    path.mkdir()
    return path


@pytest.fixture
def mdir(tmp_path: Path) -> str:
    return str(tmp_path / "manifests")


def run(cmd: str, project: Path, mdir: str, *extra: str) -> tuple[int, str]:
    result = runner.invoke(app, [cmd, "--project", str(project), "--manifest-dir", mdir, *extra])
    return result.exit_code, result.output


def settings_file(project: Path) -> Path:
    return project / ".claude" / "settings.local.json"


def test_round_trip_without_prior_file(project: Path, mdir: str) -> None:
    assert run("install", project, mdir)[0] == 0
    data = json.loads(settings_file(project).read_text())
    assert data["env"]["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")
    assert data["env"]["CLAUDE_CODE_GATEWAY_HINT_HEADERS"] == "1"
    assert set(data["hooks"]) == {"UserPromptSubmit", "PreCompact"}
    assert run("uninstall", project, mdir)[0] == 0
    assert not (project / ".claude").exists()
    assert list(Path(mdir).iterdir()) == []


def test_round_trip_keeps_preexisting_claude_dir(project: Path, mdir: str) -> None:
    (project / ".claude").mkdir()
    run("install", project, mdir)
    run("uninstall", project, mdir)
    assert (project / ".claude").is_dir()
    assert not settings_file(project).exists()


@pytest.mark.parametrize("times", [1, 2])
def test_round_trip_byte_identical_for_messy_file(project: Path, mdir: str, times: int) -> None:
    settings_file(project).parent.mkdir()
    settings_file(project).write_bytes(MESSY)
    for _ in range(times):
        assert run("install", project, mdir)[0] == 0
    data = json.loads(settings_file(project).read_text())
    assert data["env"]["MINE"] == "1"
    assert data["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"] == "echo hi"
    assert len(data["hooks"]["UserPromptSubmit"]) == 2
    assert run("uninstall", project, mdir)[0] == 0
    assert settings_file(project).read_bytes() == MESSY


def test_user_edit_survives_uninstall(project: Path, mdir: str) -> None:
    settings_file(project).parent.mkdir()
    settings_file(project).write_bytes(MESSY)
    run("install", project, mdir)
    data = json.loads(settings_file(project).read_text())
    data["permissions"] = {"allow": ["Bash(ls)"]}
    settings_file(project).write_text(json.dumps(data))
    code, out = run("uninstall", project, mdir)
    assert code == 0
    after = json.loads(settings_file(project).read_text())
    assert after["permissions"] == {"allow": ["Bash(ls)"]}
    assert "ANTHROPIC_BASE_URL" not in after["env"]
    assert len(after["hooks"]["UserPromptSubmit"]) == 1
    assert "PreCompact" not in after["hooks"]
    assert "kept" in out


def test_refuses_foreign_base_url_without_force(project: Path, mdir: str) -> None:
    raw = b'{"env": {"ANTHROPIC_BASE_URL": "https://other.example"}}'
    settings_file(project).parent.mkdir()
    settings_file(project).write_bytes(raw)
    code, _ = run("install", project, mdir)
    assert code == 1
    assert settings_file(project).read_bytes() == raw
    assert run("install", project, mdir, "--force")[0] == 0
    assert json.loads(settings_file(project).read_text())["env"]["ANTHROPIC_BASE_URL"].startswith(
        "http://127"
    )
    run("uninstall", project, mdir)
    assert settings_file(project).read_bytes() == raw


def test_invalid_json_is_untouched(project: Path, mdir: str) -> None:
    settings_file(project).parent.mkdir()
    settings_file(project).write_bytes(b"{nope")
    code, _ = run("install", project, mdir)
    assert code == 1
    assert settings_file(project).read_bytes() == b"{nope"
    assert not Path(mdir).exists()


def test_uninstall_without_install_is_noop(project: Path, mdir: str) -> None:
    code, out = run("uninstall", project, mdir)
    assert code == 0
    assert "nothing to uninstall" in out
    assert not (project / ".claude").exists()


def test_nondefault_port_adds_port_env() -> None:
    assert "SMARTROUTER_PORT" not in router_entries(Path("/r"), 8787).env
    entries = router_entries(Path("/r"), 9000)
    assert entries.env["SMARTROUTER_PORT"] == "9000"
    assert entries.env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9000"


def test_pure_apply_remove_round_trip() -> None:
    entries = router_entries(Path("/r"), 8787)
    base = {"env": {}, "hooks": {"Stop": []}}
    assert remove(apply(base, entries), entries, base) == base
    assert remove(apply({}, entries), entries, {}) == {}


def test_hook_command_is_nonblocking_when_script_is_missing() -> None:
    entries: Entries = router_entries(Path("/definitely/not/here"), 8787)
    for command in entries.hooks.values():
        done = subprocess.run(["sh", "-c", command], capture_output=True, text=True, input="{}")
        assert done.returncode == 0
        assert done.stdout == ""


def test_help_works() -> None:
    for args in (["--help"], ["serve", "--help"]):
        assert runner.invoke(app, args).exit_code == 0


def test_install_refuses_a_missing_project_or_a_claude_file(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    with pytest.raises(installer.InstallError, match="not an existing directory"):
        installer.install(missing, tmp_path, 8787, tmp_path / "m")
    assert not missing.exists()
    project = tmp_path / "p"
    project.mkdir()
    (project / ".claude").write_text("x")
    with pytest.raises(installer.InstallError, match="is not a directory"):
        installer.install(project, tmp_path, 8787, tmp_path / "m")
    assert (project / ".claude").read_text() == "x"
