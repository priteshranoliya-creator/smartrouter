import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from smartrouter import cli
from smartrouter.eval.dataset import DatasetError, GoldenItem, load_dataset, validate
from smartrouter.settings import load_settings

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "golden_small.jsonl"
TIERS = ["haiku", "sonnet", "opus"]
runner = CliRunner()


def item(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "g-0001",
        "prompt": "p",
        "context": "",
        "reference_answer": "a",
        "rubric": ["one", "two"],
        "expected_tier": "haiku",
        "task_type": "edit",
        "split": "train",
        "tags": [],
    }
    return base | overrides


def write(path: Path, rows: list[object]) -> Path:
    path.write_text("\n".join(r if isinstance(r, str) else json.dumps(r) for r in rows) + "\n")
    return path


def models(split: str, tier: str, n: int, start: int = 1) -> list[GoldenItem]:
    return [
        GoldenItem.model_validate(item(id=f"g-{start + k:04d}", split=split, expected_tier=tier))
        for k in range(n)
    ]


def test_fixture_loads() -> None:
    items = load_dataset(FIXTURE, TIERS)
    assert [i.id for i in items] == [f"g-000{n}" for n in range(1, 7)]
    assert items[0].status == "reviewed"
    assert items[0].context.startswith("def f")


def test_missing_file() -> None:
    with pytest.raises(DatasetError, match="file not found"):
        load_dataset(Path("/nonexistent/golden.jsonl"), TIERS)


def test_all_errors_reported_at_once_with_line_numbers(tmp_path: Path) -> None:
    path = write(
        tmp_path / "g.jsonl",
        [
            item(),
            "{not json",
            item(id="g-0002", expected_tier="platinum"),
            item(id="g-0001"),
            item(id="x-1", rubric=["only one"]),
            "[1, 2]",
            item(id="g-0007", surprise=True),
        ],
    )
    with pytest.raises(DatasetError) as caught:
        load_dataset(path, TIERS)
    lines = str(caught.value).splitlines()
    by_line = {n: [ln for ln in lines if ln.startswith(f"{path}:{n}:")] for n in (2, 3, 4, 5, 6, 7)}
    assert "json:" in by_line[2][0]
    assert "expected_tier: unknown tier 'platinum' (known: haiku, opus, sonnet)" in by_line[3][0]
    assert "id: duplicate of line 1" in by_line[4][0]
    assert {ln.split(": ")[1] for ln in by_line[5]} == {"id", "rubric"}
    assert "expected an object" in by_line[6][0]
    assert "surprise: Extra inputs are not permitted" in by_line[7][0]


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"prompt": ""}, "prompt"),
        ({"reference_answer": ""}, "reference_answer"),
        ({"rubric": ["a"]}, "rubric"),
        ({"rubric": ["a"] * 6}, "rubric"),
        ({"rubric": ["a", ""]}, "rubric.1"),
        ({"task_type": "chat"}, "task_type"),
        ({"split": "val"}, "split"),
        ({"status": "final"}, "status"),
        ({"id": "g-12"}, "id"),
    ],
)
def test_schema_violations_name_the_field(tmp_path: Path, overrides: dict[str, object], field: str) -> None:
    path = write(tmp_path / "g.jsonl", [item(**overrides)])
    with pytest.raises(DatasetError, match=rf"g\.jsonl:1: {field}: "):
        load_dataset(path, TIERS)


def test_status_and_source_default(tmp_path: Path) -> None:
    loaded = load_dataset(write(tmp_path / "g.jsonl", [item()]), TIERS)[0]
    assert (loaded.status, loaded.source) == ("reviewed", "")


def test_validate_counts_and_clean_report() -> None:
    items = [
        *models("train", "haiku", 3, 1),
        *models("test", "haiku", 1, 4),
        *models("train", "sonnet", 3, 5),
        *models("test", "sonnet", 1, 8),
        *models("train", "opus", 3, 9),
        *models("test", "opus", 2, 12),
    ]
    report = validate(items, TIERS)
    assert report.ok
    assert report.problems == []
    assert report.tier_split[("opus", "test")] == 2
    assert report.task_types == {"edit": 13}
    assert report.total == 13
    assert report.drafts == 0
    assert report.warnings == []


def test_validate_counts_drafts_and_task_types() -> None:
    items = [
        GoldenItem.model_validate(item(id="g-0001", status="draft", task_type="debug")),
        GoldenItem.model_validate(item(id="g-0002", split="test", task_type="plan")),
    ]
    report = validate(items)
    assert report.drafts == 1
    assert report.task_types == {"debug": 1, "plan": 1}


def test_validate_errors() -> None:
    only_train = validate(models("train", "haiku", 4), TIERS)
    assert "no test items" in only_train.problems
    assert "tier 'opus' has no test items" in only_train.problems
    only_test = validate(models("test", "haiku", 4), TIERS)
    assert "no train items" in only_test.problems
    missing_tier = validate([*models("train", "haiku", 3), *models("test", "haiku", 1, 4)], TIERS)
    assert missing_tier.problems == [
        "tier 'sonnet' has no test items",
        "tier 'opus' has no test items",
    ]


def test_validate_warnings_do_not_fail() -> None:
    items = [
        *models("train", "haiku", 8, 1),
        *models("test", "haiku", 4, 9),
        *models("train", "sonnet", 4, 13),
        *models("test", "sonnet", 2, 17),
        *models("test", "opus", 1, 19),
    ]
    report = validate(items, TIERS)
    assert report.ok
    assert any("tier 'opus' is 5%" in w for w in report.warnings)
    assert any("test share is 37%" in w for w in report.warnings)
    heavy = validate([*models("train", "haiku", 1), *models("test", "haiku", 3, 2)], ["haiku"])
    assert heavy.warnings == ["test share is 75% (expected 25% to 35%)"]


def test_validate_empty() -> None:
    report = validate([], TIERS)
    assert not report.ok
    assert report.warnings == []


@pytest.fixture
def settings_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load_settings(env={})
    monkeypatch.setattr(cli, "load_settings", lambda: settings)


def test_cli_validate_ok(settings_cli: None) -> None:
    result = runner.invoke(cli.app, ["eval", "validate", "--dataset", str(FIXTURE)])
    assert result.exit_code == 0, result.output
    assert "haiku" in result.output
    assert "items: 6  drafts: 0" in result.output
    assert "warning: test share is 50%" in result.output
    assert result.output.rstrip().endswith("ok")


def test_cli_validate_schema_errors_exit_1(settings_cli: None, tmp_path: Path) -> None:
    path = write(tmp_path / "g.jsonl", [item(expected_tier="nope")])
    result = runner.invoke(cli.app, ["eval", "validate", "--dataset", str(path)])
    assert result.exit_code == 1
    assert f"{path}:1: expected_tier: unknown tier 'nope'" in result.output


def test_cli_validate_split_problems_exit_1(settings_cli: None, tmp_path: Path) -> None:
    path = write(tmp_path / "g.jsonl", [item()])
    result = runner.invoke(cli.app, ["eval", "validate", "--dataset", str(path)])
    assert result.exit_code == 1
    assert "error: no test items" in result.output


def test_cli_validate_missing_file_exit_1(settings_cli: None, tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["eval", "validate", "--dataset", str(tmp_path / "none.jsonl")])
    assert result.exit_code == 1
    assert "file not found" in result.output
