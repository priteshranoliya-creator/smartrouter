import plistlib
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from smartrouter.service import LABEL, Service, ServiceError, render_plist


class FakeLaunchctl:
    """Records argv; `fail` maps a launchctl verb to the stderr it fails with."""

    def __init__(self, fail: dict[str, str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail or {}

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        self.calls.append(argv)
        verb = argv[1]
        if verb in self.fail:
            return subprocess.CompletedProcess(argv, 5, "", self.fail[verb])
        return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    program = tmp_path / "repo" / ".venv" / "bin" / "router"
    program.parent.mkdir(parents=True)
    program.touch()
    return tmp_path / "repo"


def service(tmp_path: Path, root: Path, launchctl: FakeLaunchctl, platform: str = "darwin") -> Service:
    return Service(
        root=root,
        agents_dir=tmp_path / "LaunchAgents",
        log_dir=tmp_path / "logs",
        run=launchctl,
        platform=platform,
        uid=501,
    )


def test_plist_runs_the_venv_router_from_the_repo_and_keeps_it_alive(tmp_path: Path) -> None:
    plist = plistlib.loads(render_plist(Path("/r"), Path("/r/data/logs")))
    assert plist["Label"] == LABEL
    assert plist["ProgramArguments"] == ["/r/.venv/bin/router", "serve"]
    assert plist["WorkingDirectory"] == "/r"
    assert plist["RunAtLoad"] is True and plist["KeepAlive"] is True
    assert plist["ThrottleInterval"] > 0
    assert plist["StandardErrorPath"] == "/r/data/logs/router.err.log"


def test_install_writes_the_plist_and_bootstraps_it(tmp_path: Path, root: Path) -> None:
    launchctl = FakeLaunchctl()
    svc = service(tmp_path, root, launchctl)
    svc.install()
    assert plistlib.loads(svc.plist.read_bytes())["ProgramArguments"][0] == str(root / ".venv/bin/router")
    assert (tmp_path / "logs").is_dir()
    assert launchctl.calls == [
        ["launchctl", "bootout", f"gui/501/{LABEL}"],
        ["launchctl", "bootstrap", "gui/501", str(svc.plist)],
    ]
    assert [p.name for p in svc.agents_dir.iterdir()] == [f"{LABEL}.plist"]  # no temp file left behind


def test_install_is_repeatable(tmp_path: Path, root: Path) -> None:
    svc = service(tmp_path, root, FakeLaunchctl(fail={"bootout": "No such process"}))
    svc.install()
    svc.install()
    assert svc.plist.exists()


def test_install_reports_a_bootstrap_failure(tmp_path: Path, root: Path) -> None:
    svc = service(tmp_path, root, FakeLaunchctl(fail={"bootstrap": "Input/output error"}))
    with pytest.raises(ServiceError, match="Input/output error"):
        svc.install()
    assert list(svc.agents_dir.iterdir()) == []  # nothing left for launchd to retry at login


def test_install_needs_the_venv(tmp_path: Path) -> None:
    with pytest.raises(ServiceError, match="uv sync"):
        service(tmp_path, tmp_path / "empty", FakeLaunchctl()).install()


def test_uninstall_boots_out_and_deletes(tmp_path: Path, root: Path) -> None:
    launchctl = FakeLaunchctl()
    svc = service(tmp_path, root, launchctl)
    svc.install()
    assert svc.uninstall().startswith("removed")
    assert not svc.plist.exists()
    assert launchctl.calls[-1] == ["launchctl", "bootout", f"gui/501/{LABEL}"]


def test_uninstall_when_absent(tmp_path: Path, root: Path) -> None:
    svc = service(tmp_path, root, FakeLaunchctl(fail={"bootout": "No such process"}))
    assert svc.uninstall() == "not installed"


@pytest.mark.parametrize(("fail", "expected"), [({}, True), ({"print": "not found"}, False)])
def test_loaded(tmp_path: Path, root: Path, fail: dict[str, str], expected: bool) -> None:
    assert service(tmp_path, root, FakeLaunchctl(fail=fail)).loaded() is expected


@pytest.mark.parametrize("action", ["install", "uninstall", "loaded"])
def test_refuses_off_macos(tmp_path: Path, root: Path, action: str) -> None:
    launchctl = FakeLaunchctl()
    with pytest.raises(ServiceError, match="macOS only"):
        getattr(service(tmp_path, root, launchctl, platform="linux"), action)()
    assert launchctl.calls == []
