import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from smartrouter import cli
from smartrouter.decision.calibrated import (
    CalibratedEngine,
    Calibration,
    TrainLoss,
    read_calibration,
    write_calibration,
)
from smartrouter.decision.engine import DecisionState, EngineError, FakeEngine
from smartrouter.decision.factory import build_engine
from smartrouter.settings import Engine, Settings, load_settings
from tests.unit.eval_fakes import item

BASE = load_settings(env={})
LABELS = {
    "easy": {"A": 0.6, "B": 0.2, "C": 0.1, "D": 0.1},
    "mid": {"A": 0.1, "B": 0.4, "C": 0.3, "D": 0.2},
    "hard": {"A": 0.05, "B": 0.15, "C": 0.1, "D": 0.7},
}
TRAIN = [
    (1, "haiku", "easy"),
    (2, "haiku", "easy"),
    (3, "sonnet", "easy"),
    (4, "sonnet", "mid"),
    (5, "sonnet", "mid"),
    (6, "opus", "hard"),
    (7, "opus", "hard"),
    (8, "sonnet", "mid"),
]
TEST = [
    (9, "haiku", "easy"),
    (10, "sonnet", "mid"),
    (11, "sonnet", "mid"),
    (12, "opus", "hard"),
    (13, "sonnet", "easy"),
]
runner = CliRunner()


def respond(state: DecisionState) -> dict[str, dict[str, float]]:
    marker = state.prompt.split()[0]
    if marker == "boom":
        raise EngineError("engine down")
    return {"complexity": LABELS[marker]}


def write_dataset(path: Path, rows: list[tuple[int, str, str, str]]) -> None:
    path.write_text(
        "".join(item(n, tier, marker, split).model_dump_json() + "\n" for n, tier, marker, split in rows)
    )


def rows(test: list[tuple[int, str, str]] = TEST) -> list[tuple[int, str, str, str]]:
    return [(*r, "train") for r in TRAIN] + [(*r, "test") for r in test]


def with_paths(tmp_path: Path, engine: Engine = Engine.FAKE) -> Settings:
    paths = BASE.router.paths.model_copy(
        update={
            "calibration": tmp_path / "calibration.json",
            "reports": tmp_path / "reports",
            "db": tmp_path / "r.db",
        }
    )
    decision = BASE.router.decision.model_copy(update={"engine": engine})
    router = BASE.router.model_copy(update={"paths": paths, "decision": decision})
    return Settings(router=router, questions=BASE.questions, secrets=BASE.secrets)


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine(BASE.questions, respond)


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, engine: FakeEngine) -> Settings:
    settings = with_paths(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "build_raw_engine", lambda s, http: engine)
    return settings


def calibrate(dataset: Path, *extra: str):  # type: ignore[no-untyped-def]
    return runner.invoke(cli.app, ["eval", "calibrate", "--dataset", str(dataset), *extra])


def test_calibrate_writes_the_file_the_raw_cache_and_the_report(
    wired: Settings, tmp_path: Path, engine: FakeEngine
) -> None:
    dataset = tmp_path / "g.jsonl"
    write_dataset(dataset, rows())

    result = calibrate(dataset)

    assert result.exit_code == 0, result.output
    saved = json.loads((tmp_path / "calibration.json").read_text())
    assert set(saved) == {
        "engine",
        "question",
        "temperature",
        "n_train",
        "fitted_at",
        "train",
        "dataset_version",
    }
    assert saved["engine"] == "fake"
    assert saved["question"] == "complexity"
    assert saved["n_train"] == len(TRAIN)
    assert set(saved["train"]) == {"nll_before", "nll_after"}
    assert saved["train"]["nll_after"] <= saved["train"]["nll_before"]
    assert len(engine.calls) == len(TRAIN) + len(TEST)
    cached = (tmp_path / "calibration_raw.jsonl").read_text().splitlines()
    assert len(cached) == len(TRAIN) + len(TEST)
    (report,) = (tmp_path / "reports").glob("calibration-*.md")
    text = report.read_text()
    assert text.strip() == result.output.strip()
    assert "| accuracy |" in text
    assert "| ECE (10 bins) |" in text
    assert "| NLL (expected tier) |" in text
    assert "majority-class baseline" in text
    assert text.count("| expected \\ predicted | haiku | sonnet | opus |") == 2


def test_report_baseline_is_the_train_majority_applied_to_test(wired: Settings, tmp_path: Path) -> None:
    dataset = tmp_path / "g.jsonl"
    write_dataset(dataset, rows())
    result = calibrate(dataset)
    # train has 2 haiku, 4 sonnet, 2 opus -> sonnet; test has 3 sonnet of 5
    assert "| accuracy |" in result.output
    accuracy_line = next(line for line in result.output.splitlines() if line.startswith("| accuracy |"))
    assert accuracy_line.split("|")[4].strip() == "0.600"
    assert "always answers `sonnet`" in result.output


def test_second_run_makes_no_engine_calls(wired: Settings, tmp_path: Path, engine: FakeEngine) -> None:
    dataset = tmp_path / "g.jsonl"
    write_dataset(dataset, rows())
    calibrate(dataset)
    first = json.loads((tmp_path / "calibration.json").read_text())["temperature"]
    engine.calls.clear()

    second = calibrate(dataset)

    assert second.exit_code == 0, second.output
    assert engine.calls == []
    assert json.loads((tmp_path / "calibration.json").read_text())["temperature"] == first
    assert len((tmp_path / "calibration_raw.jsonl").read_text().splitlines()) == len(TRAIN) + len(TEST)


def test_temperature_is_fitted_on_train_only(wired: Settings, tmp_path: Path) -> None:
    dataset = tmp_path / "g.jsonl"
    write_dataset(dataset, rows())
    calibrate(dataset)
    first = json.loads((tmp_path / "calibration.json").read_text())["temperature"]

    flipped = [(9, "opus", "easy"), (10, "haiku", "mid"), *TEST[2:]]
    write_dataset(dataset, rows(flipped))
    calibrate(dataset)
    second = json.loads((tmp_path / "calibration.json").read_text())["temperature"]

    assert second == first


def test_engine_errors_are_counted_and_skipped(wired: Settings, tmp_path: Path) -> None:
    dataset = tmp_path / "g.jsonl"
    write_dataset(dataset, rows([*TEST[:3], (12, "opus", "boom")]))

    result = calibrate(dataset)

    assert result.exit_code == 0, result.output
    assert "Engine errors skipped: test 1" in result.output
    assert len((tmp_path / "calibration_raw.jsonl").read_text().splitlines()) == len(TRAIN) + 3


def test_a_split_with_no_usable_items_fails_without_writing(wired: Settings, tmp_path: Path) -> None:
    dataset = tmp_path / "g.jsonl"
    write_dataset(dataset, rows([(12, "opus", "boom")]))

    result = calibrate(dataset)

    assert result.exit_code == 1
    assert "no usable test items" in result.output
    assert not (tmp_path / "calibration.json").exists()


def test_split_option_rejects_unknown_values(wired: Settings, tmp_path: Path) -> None:
    dataset = tmp_path / "g.jsonl"
    write_dataset(dataset, rows())
    assert calibrate(dataset, "--split", "dev").exit_code == 2


def make_calibration(engine: str = "fake", temperature: float = 2.0) -> Calibration:
    return Calibration(
        engine=engine,
        question="complexity",
        temperature=temperature,
        n_train=8,
        fitted_at="2026-10-19T12:00:00+00:00",
        train=TrainLoss(nll_before=1.0, nll_after=0.8),
        dataset_version="abc123def456",
    )


async def test_calibrated_engine_rescales_the_routing_question_and_keeps_the_name() -> None:
    inner = FakeEngine(
        BASE.questions,
        lambda s: {
            "complexity": {"A": 0.5, "B": 0.3, "C": 0.1, "D": 0.1},
            "task_type": {"E": 0.7, "D": 0.3},
        },
    )
    wrapped = CalibratedEngine(inner, make_calibration(temperature=0.5))

    decision = await wrapped.decide(DecisionState("x"))

    assert wrapped.name == inner.name
    assert wrapped.state_budget_tokens == inner.state_budget_tokens
    assert decision.engine == "fake"
    complexity = decision.answers["complexity"]
    assert complexity.choice == "A"
    assert complexity.probabilities["A"] == pytest.approx(0.25 / 0.36, abs=1e-5)
    assert complexity.probabilities["B"] == pytest.approx(0.09 / 0.36, abs=1e-5)
    assert decision.answers["task_type"].probabilities == {"E": 0.7, "D": 0.3}


async def test_factory_wraps_only_when_the_calibration_engine_matches(tmp_path: Path) -> None:
    settings = with_paths(tmp_path)
    path = settings.path("calibration")
    async with httpx.AsyncClient() as http:
        assert isinstance(build_engine(settings, http), FakeEngine)

        write_calibration(path, make_calibration(engine="scaledown"))
        assert isinstance(build_engine(settings, http), FakeEngine)

        write_calibration(path, make_calibration(engine="fake"))
        wrapped = build_engine(settings, http)
        assert isinstance(wrapped, CalibratedEngine)
        assert wrapped.name == "fake"

        path.write_text("{not json")
        assert isinstance(build_engine(settings, http), FakeEngine)


def test_calibration_file_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "calibration.json"
    assert read_calibration(path) is None
    write_calibration(path, make_calibration())
    assert read_calibration(path) == make_calibration()
