import csv
import io
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from smartrouter import cli
from smartrouter.app import create_app
from smartrouter.settings import Mode, Settings, load_settings
from smartrouter.telemetry import stats
from smartrouter.telemetry.export import COLUMNS, write_csv
from smartrouter.telemetry.repo import Repo
from tests.seeded import HAIKU, OPUS, SONNET, seed

NOW = datetime.now(UTC)
runner = CliRunner()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    base = load_settings(env={})
    paths = base.router.paths.model_copy(update={"db": tmp_path / "router.db"})
    router = base.router.model_copy(update={"paths": paths, "mode": Mode.OFF})
    return Settings(router=router, questions=base.questions, secrets=base.secrets)


@pytest.fixture
def seeded(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    with Repo.open(settings.path("db")) as repo:
        seed(repo, NOW)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    yield settings


def export_rows(args: list[str]) -> list[dict[str, str]]:
    result = runner.invoke(cli.app, ["export", *args])
    assert result.exit_code == 0, result.output
    return list(csv.DictReader(io.StringIO(result.stdout)))


def column_sum(rows: list[dict[str, str]], column: str) -> float:
    return sum(float(r[column]) for r in rows)


def test_header_and_one_row_per_request(seeded: Settings) -> None:
    result = runner.invoke(cli.app, ["export"])
    assert result.exit_code == 0
    assert result.stdout.splitlines()[0] == ",".join(COLUMNS)
    assert len(result.stdout.splitlines()) == 1 + 8


def test_rows_carry_the_decision_and_the_baseline_columns(seeded: Settings) -> None:
    rows = export_rows([])
    first, *_, last = rows
    assert first["session_id"] == "sess-b"  # the 10-day-old request, oldest first
    assert (first["turn_seq"], first["kind"], first["mode"], first["rule"]) == (
        "1",
        "main",
        "shadow",
        "upgrade",
    )
    assert (first["chosen_model"], first["requested_model"], first["sent_model"]) == (OPUS, HAIKU, HAIKU)
    assert (first["cost_usd"], first["counterfactual_cost_usd"]) == ("0.1", "0.8")
    assert (first["without_router_usd"], first["with_router_usd"]) == ("0.1", "0.8")
    assert (last["session_id"], last["turn_seq"], last["mode"], last["rule"]) == (
        "sess-a",
        "3",
        "shadow",
        "decision_timeout",
    )
    background = next(r for r in rows if r["kind"] == "background")
    assert background["counterfactual_cost_usd"] == ""
    assert (background["without_router_usd"], background["with_router_usd"]) == ("0.05", "0.05")
    active = next(r for r in rows if r["mode"] == "active")
    assert (active["requested_model"], active["sent_model"], active["chosen_model"]) == (OPUS, SONNET, SONNET)
    assert (active["without_router_usd"], active["with_router_usd"]) == ("4.0", "1.0")
    assert (active["input_tokens"], active["cache_write_1h_tokens"]) == ("500", "500")


@pytest.mark.parametrize(("window", "count"), [("all", 8), ("30d", 8), ("24h", 7), ("1h", 6), ("15m", 2)])
def test_export_totals_match_the_stats_api(seeded: Settings, window: str, count: int) -> None:
    rows = export_rows(["--since", window])
    with TestClient(create_app(seeded)) as client:
        api = client.get("/api/stats", params={"window": window}).json()
    assert len(rows) == count == api["requests"]
    assert column_sum(rows, "cost_usd") == pytest.approx(api["spend_usd"])
    assert column_sum(rows, "without_router_usd") == pytest.approx(api["without_router_usd"])
    assert column_sum(rows, "with_router_usd") == pytest.approx(api["with_router_usd"])


def test_export_reproduces_the_hand_computed_totals(seeded: Settings) -> None:
    rows = export_rows(["--since", "all"])
    assert column_sum(rows, "cost_usd") == pytest.approx(10.15)
    assert column_sum(rows, "without_router_usd") == pytest.approx(13.15)
    assert column_sum(rows, "with_router_usd") == pytest.approx(10.25)


def test_out_writes_a_file_instead_of_stdout(seeded: Settings, tmp_path: Path) -> None:
    target = tmp_path / "nested.csv"
    result = runner.invoke(cli.app, ["export", "--since", "1h", "--out", str(target)])
    assert result.exit_code == 0
    assert result.stdout == ""
    assert len(list(csv.DictReader(target.open()))) == 6


def test_unknown_window_exits_2(seeded: Settings) -> None:
    result = runner.invoke(cli.app, ["export", "--since", "2h"])
    assert result.exit_code == 2
    assert "unknown window" in result.output


def test_missing_database_exits_1_without_creating_one(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    result = runner.invoke(cli.app, ["export"])
    assert result.exit_code == 1
    assert "no database" in result.output
    assert not settings.path("db").exists()


def test_write_csv_reports_the_count(tmp_path: Path) -> None:
    with Repo.open(tmp_path / "r.db") as repo:
        seed(repo, NOW)
        rows = repo.request_rows(stats.window_since("15m", NOW))
    out = io.StringIO()
    assert write_csv(rows, out) == 2
    assert out.getvalue().count("\n") == 3
