"""Fit the routing question's temperature on one split and score it on the other.

Raw engine answers are cached on disk, so a rerun costs no engine calls. Only the fit split ever
reaches `fit_temperature`.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from smartrouter.decision.calibrated import Calibration, TrainLoss
from smartrouter.decision.calibration import (
    Sample,
    accuracy,
    apply_temperature,
    confusion,
    ece,
    fit_temperature,
    nll,
    sum_by_tier,
    tier_nll,
)
from smartrouter.decision.engine import DecisionEngine, EngineError
from smartrouter.decision.features import build_state
from smartrouter.eval.dataset import GoldenItem
from smartrouter.eval.router_arm import item_text

CacheKey = tuple[str, str, str, str]  # item id, engine name, dataset version, question


class CalibrationRunError(Exception):
    """The run cannot produce a calibration (empty split, every engine call failed)."""


@dataclass(frozen=True)
class Metrics:
    n: int
    accuracy: float
    ece: float
    nll: float
    confusion: dict[str, dict[str, int]]


@dataclass(frozen=True)
class CalibrationRun:
    calibration: Calibration
    fit_split: str
    eval_split: str
    before: Metrics
    after: Metrics
    majority_tier: str
    majority_accuracy: float
    errors: Mapping[str, int]
    engine_calls: int
    tiers: tuple[str, ...]


def read_cache(path: Path) -> dict[CacheKey, dict[str, float]]:
    rows: dict[CacheKey, dict[str, float]] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return rows
    for line in text.splitlines():
        try:
            raw = json.loads(line)
            key = (raw["item"], raw["engine"], raw["dataset_version"], raw["question"])
            rows[key] = {str(k): float(v) for k, v in raw["probabilities"].items()}
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    return rows


def append_cache(path: Path, key: CacheKey, probabilities: Mapping[str, float]) -> None:
    item, engine, version, question = key
    row = {
        "item": item,
        "engine": engine,
        "dataset_version": version,
        "question": question,
        "probabilities": dict(probabilities),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


async def collect_raw(
    engine: DecisionEngine,
    items: Sequence[GoldenItem],
    *,
    question: str,
    labels: Sequence[str],
    version: str,
    cache: Path,
    concurrency: int,
) -> tuple[dict[str, dict[str, float]], Counter[str], int]:
    """Raw label probabilities by item id, errors by split, and the number of engine calls made."""
    cached = read_cache(cache)
    known = set(labels)
    out: dict[str, dict[str, float]] = {}
    errors: Counter[str] = Counter()
    todo: list[GoldenItem] = []
    for item in items:
        hit = cached.get((item.id, engine.name, version, question))
        if hit is None:
            todo.append(item)
        else:
            out[item.id] = hit
    gate = asyncio.Semaphore(concurrency)

    async def one(item: GoldenItem) -> None:
        async with gate:
            try:
                decision = await engine.decide(build_state(item_text(item), None, engine.state_budget_tokens))
            except EngineError:
                errors[item.split] += 1
                return
        answer = decision.answers.get(question)
        if answer is None or not answer.probabilities or not set(answer.probabilities) <= known:
            errors[item.split] += 1
            return
        out[item.id] = dict(answer.probabilities)
        append_cache(cache, (item.id, engine.name, version, question), answer.probabilities)

    await asyncio.gather(*(one(item) for item in todo))
    return out, errors, len(todo)


def metrics(
    samples: Sequence[Sample],
    tier_map: Mapping[str, str],
    tiers: Sequence[str],
    temperature: float | None = None,
) -> Metrics:
    """Tier-level accuracy, ECE, NLL and confusion. `temperature` None scores the raw probabilities."""
    scored = [
        sum_by_tier(labels if temperature is None else apply_temperature(labels, temperature), tier_map)
        for labels, _ in samples
    ]
    expected = [e for _, e in samples]
    predicted = [max(tiers, key=lambda t: p.get(t, 0.0)) for p in scored]
    confidences = [max(p.get(t, 0.0) for t in tiers) for p in scored]
    correct = [e == p for e, p in zip(expected, predicted, strict=True)]
    return Metrics(
        n=len(samples),
        accuracy=accuracy(expected, predicted),
        ece=ece(confidences, correct),
        nll=nll(scored, expected),
        confusion=confusion(expected, predicted, tiers),
    )


async def run_calibration(
    engine: DecisionEngine,
    items: Sequence[GoldenItem],
    *,
    question: str,
    tier_map: Mapping[str, str],
    tiers: Sequence[str],
    version: str,
    cache: Path,
    fit_split: str,
    concurrency: int,
    now: datetime,
) -> CalibrationRun:
    eval_split = "test" if fit_split == "train" else "train"
    raw, errors, calls = await collect_raw(
        engine,
        items,
        question=question,
        labels=list(tier_map),
        version=version,
        cache=cache,
        concurrency=concurrency,
    )

    def samples(split: str) -> list[Sample]:
        return [(raw[i.id], i.expected_tier) for i in items if i.split == split and i.id in raw]

    fit, held_out = samples(fit_split), samples(eval_split)
    for split, rows in ((fit_split, fit), (eval_split, held_out)):
        if not rows:
            raise CalibrationRunError(
                f"no usable {split} items ({errors[split]} engine errors, "
                f"{sum(1 for i in items if i.split == split)} items)"
            )
    temperature = fit_temperature(fit, tier_map)
    calibration = Calibration(
        engine=engine.name,
        question=question,
        temperature=temperature,
        n_train=len(fit),
        fitted_at=now.isoformat(timespec="seconds"),
        train=TrainLoss(nll_before=tier_nll(fit, tier_map), nll_after=tier_nll(fit, tier_map, temperature)),
        dataset_version=version,
    )
    majority = max(tiers, key=Counter(e for _, e in fit).__getitem__)
    held_out_expected = [e for _, e in held_out]
    return CalibrationRun(
        calibration=calibration,
        fit_split=fit_split,
        eval_split=eval_split,
        before=metrics(held_out, tier_map, tiers),
        after=metrics(held_out, tier_map, tiers, temperature),
        majority_tier=majority,
        majority_accuracy=accuracy(held_out_expected, [majority] * len(held_out_expected)),
        errors=dict(errors),
        engine_calls=calls,
        tiers=tuple(tiers),
    )


def _confusion_table(title: str, table: Mapping[str, Mapping[str, int]], tiers: Sequence[str]) -> list[str]:
    lines = [f"**{title}** (rows expected, columns predicted)", ""]
    lines.append("| expected \\ predicted | " + " | ".join(tiers) + " |")
    lines.append("| --- | " + " | ".join("---:" for _ in tiers) + " |")
    lines.extend(f"| {e} | " + " | ".join(str(table[e][p]) for p in tiers) + " |" for e in tiers)
    return lines


def render_report(run: CalibrationRun, dataset: str) -> str:
    cal = run.calibration
    before, after = run.before, run.after
    errors = ", ".join(f"{split} {n}" for split, n in sorted(run.errors.items())) or "none"
    lines = [
        f"# Calibration: {cal.engine}",
        "",
        f"- Question: `{cal.question}`",
        f"- Dataset: `{dataset}` (version `{cal.dataset_version}`)",
        f"- Fitted at: {cal.fitted_at}",
        f"- Fit split: {run.fit_split} ({cal.n_train} items)",
        f"- Evaluated on: {run.eval_split} ({before.n} items)",
        f"- Temperature: {cal.temperature:.4f}",
        f"- Engine errors skipped: {errors}",
        f"- Engine calls this run: {run.engine_calls}",
        "",
        f"## {run.eval_split.capitalize()} metrics",
        "",
        "| metric | before | after | majority-class baseline |",
        "| --- | ---: | ---: | ---: |",
        f"| accuracy | {before.accuracy:.3f} | {after.accuracy:.3f} | {run.majority_accuracy:.3f} |",
        f"| ECE (10 bins) | {before.ece:.3f} | {after.ece:.3f} | - |",
        f"| NLL (expected tier) | {before.nll:.3f} | {after.nll:.3f} | - |",
        "",
        f"The baseline always answers `{run.majority_tier}`, the top tier of {run.fit_split}.",
        "",
        f"## {run.fit_split.capitalize()} NLL",
        "",
        f"before {cal.train.nll_before:.3f}, after {cal.train.nll_after:.3f}",
        "",
        "## Confusion matrices",
        "",
        *_confusion_table("Before", before.confusion, run.tiers),
        "",
        *_confusion_table("After", after.confusion, run.tiers),
        "",
    ]
    return "\n".join(lines)
