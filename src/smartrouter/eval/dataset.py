"""Golden dataset: one JSON object per line, loaded and validated against the configured tiers."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

NonEmpty = Annotated[str, StringConstraints(min_length=1)]
TaskType = Literal["edit", "debug", "explain", "plan", "tests", "other"]
Split = Literal["train", "test"]
Status = Literal["draft", "reviewed"]

TASK_TYPES: tuple[str, ...] = ("edit", "debug", "explain", "plan", "tests", "other")
MIN_TIER_SHARE = 0.20
TEST_SHARE_RANGE = (0.25, 0.35)


class DatasetError(Exception):
    """The dataset file is unreadable or invalid; the message holds one problem per line."""


class GoldenItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Annotated[str, StringConstraints(pattern=r"^g-\d{4}$")]
    prompt: NonEmpty
    context: str
    reference_answer: NonEmpty
    rubric: Annotated[list[NonEmpty], Field(min_length=2, max_length=5)]
    expected_tier: NonEmpty
    task_type: TaskType
    split: Split
    tags: list[str]
    status: Status = "reviewed"
    source: str = ""


def dataset_version(path: Path) -> str:
    """First 12 hex characters of the file's SHA-256: names the exact dataset an eval run used."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except OSError as exc:
        raise DatasetError(f"{path}: cannot read file ({exc.strerror})") from None


def load_dataset(path: Path, tiers: Iterable[str]) -> list[GoldenItem]:
    known = sorted(tiers)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise DatasetError(f"{path}: file not found") from None
    except OSError as exc:
        raise DatasetError(f"{path}: cannot read file ({exc.strerror})") from None

    items: list[GoldenItem] = []
    errors: list[str] = []
    first_line: dict[str, int] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        where = f"{path}:{number}"
        try:
            raw = json.loads(line)
        except ValueError as exc:
            errors.append(f"{where}: json: {exc}")
            continue
        if not isinstance(raw, dict):
            errors.append(f"{where}: json: expected an object, got {type(raw).__name__}")
            continue
        try:
            item = GoldenItem.model_validate(raw)
        except ValidationError as exc:
            errors.extend(
                f"{where}: {'.'.join(str(part) for part in err['loc']) or 'item'}: {err['msg']}"
                for err in exc.errors()
            )
            continue
        problems = []
        if item.id in first_line:
            problems.append(f"{where}: id: duplicate of line {first_line[item.id]} ({item.id})")
        else:
            first_line[item.id] = number
        if item.expected_tier not in known:
            problems.append(
                f"{where}: expected_tier: unknown tier {item.expected_tier!r} (known: {', '.join(known)})"
            )
        errors.extend(problems)
        if not problems:
            items.append(item)
    if errors:
        raise DatasetError("\n".join(errors))
    return items


@dataclass(frozen=True)
class Report:
    total: int
    tier_split: dict[tuple[str, str], int]
    task_types: dict[str, int]
    drafts: int
    tiers: tuple[str, ...]
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def validate(items: Sequence[GoldenItem], tiers: Iterable[str] | None = None) -> Report:
    """Check the split and tier balance. `tiers` defaults to the tiers present in `items`."""
    tier_names = tuple(tiers) if tiers is not None else tuple(sorted({i.expected_tier for i in items}))
    total = len(items)
    tier_split: Counter[tuple[str, str]] = Counter((i.expected_tier, i.split) for i in items)
    test = sum(1 for i in items if i.split == "test")
    problems: list[str] = []
    warnings: list[str] = []
    if not test:
        problems.append("no test items")
    if total - test == 0:
        problems.append("no train items")
    for tier in tier_names:
        if tier_split[(tier, "test")] == 0:
            problems.append(f"tier {tier!r} has no test items")
    if total:
        for tier in tier_names:
            count = tier_split[(tier, "train")] + tier_split[(tier, "test")]
            if count / total < MIN_TIER_SHARE:
                warnings.append(f"tier {tier!r} is {count / total:.0%} of items (under {MIN_TIER_SHARE:.0%})")
        low, high = TEST_SHARE_RANGE
        if not low <= test / total <= high:
            warnings.append(f"test share is {test / total:.0%} (expected {low:.0%} to {high:.0%})")
    return Report(
        total=total,
        tier_split=dict(tier_split),
        task_types={t: n for t in TASK_TYPES if (n := sum(1 for i in items if i.task_type == t))},
        drafts=sum(1 for i in items if i.status == "draft"),
        tiers=tier_names,
        problems=problems,
        warnings=warnings,
    )
