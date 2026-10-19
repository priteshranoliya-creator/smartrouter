from pathlib import Path

import pytest
from typer.testing import CliRunner

from smartrouter import cli
from smartrouter.settings import Settings, load_settings
from smartrouter.telemetry.models import EvalRun
from smartrouter.telemetry.repo import Repo
from tests.seeded import seed_eval_run

BASE = load_settings(env={})
runner = CliRunner()


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    paths = BASE.router.paths.model_copy(update={"db": tmp_path / "r.db", "reports": tmp_path / "reports"})
    settings = Settings(
        router=BASE.router.model_copy(update={"paths": paths}),
        questions=BASE.questions,
        secrets=BASE.secrets,
    )
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    return settings


def report(*extra: str):  # type: ignore[no-untyped-def]
    return runner.invoke(cli.app, ["eval", "report", *extra])


def test_report_prints_and_writes_the_named_run(wired: Settings, tmp_path: Path) -> None:
    with Repo.open(tmp_path / "r.db") as repo:
        first = seed_eval_run(repo)
        seed_eval_run(repo)

    result = report("--run", str(first))

    assert result.exit_code == 0, result.output
    written = (tmp_path / "reports" / f"run-{first}.md").read_text()
    assert written.strip() == result.output.strip()
    assert f"# Eval run {first}" in written
    assert "Routing accuracy 75% over 4 items" in written
    assert not (tmp_path / "reports" / "run-2.md").exists()


def test_report_without_run_takes_the_latest(wired: Settings, tmp_path: Path) -> None:
    with Repo.open(tmp_path / "r.db") as repo:
        seed_eval_run(repo)
        latest = seed_eval_run(repo)

    result = report()

    assert result.exit_code == 0, result.output
    assert result.output.startswith(f"# Eval run {latest}")
    assert (tmp_path / "reports" / f"run-{latest}.md").exists()


def test_report_without_any_run_fails_cleanly(wired: Settings, tmp_path: Path) -> None:
    with Repo.open(tmp_path / "r.db"):
        pass
    result = report()
    assert result.exit_code == 1
    assert "no eval runs yet" in result.output
    assert not (tmp_path / "reports").exists()


def test_report_of_an_unknown_run_exits_2(wired: Settings, tmp_path: Path) -> None:
    with Repo.open(tmp_path / "r.db") as repo:
        repo.insert_eval_run(EvalRun(dataset_version="v1", arms=["a"], judge_model="j", started_at="t"))
    result = report("--run", "9")
    assert result.exit_code == 2
    assert "no eval run 9" in result.output


def test_report_without_a_database_fails_cleanly(wired: Settings) -> None:
    result = report()
    assert result.exit_code == 1
    assert "no database at" in result.output
