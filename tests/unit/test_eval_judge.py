import json

import pytest

from smartrouter.eval.judge import (
    JUDGE_SYSTEM_PROMPT,
    JudgeError,
    Judgment,
    build_prompt,
    in_rejudge_sample,
    judge_answer,
    parse_reply,
)
from tests.unit.eval_fakes import FakeProvider, item

ITEM = item(1, "haiku", "easy")


def reply(*verdicts: str) -> str:
    points = [{"index": i, "verdict": v, "reason": f"r{i}"} for i, v in enumerate(verdicts, start=1)]
    return json.dumps({"points": points})


def test_prompt_has_request_context_reference_numbered_rubric_and_answer() -> None:
    prompt = build_prompt(ITEM, "my candidate")
    assert "easy request" in prompt
    assert "ref" in prompt
    assert "1. point one\n2. point two" in prompt
    assert "my candidate" in prompt
    assert "JSON only" in prompt


def test_parse_plain_fenced_and_prose_wrapped_json() -> None:
    plain = reply("hit", "miss")
    assert parse_reply(plain, 2) == ((1, "hit", "r1"), (2, "miss", "r2"))
    assert parse_reply(f"```json\n{plain}\n```", 2) == parse_reply(plain, 2)
    assert parse_reply(f"```\n{plain}\n```\n", 2) == parse_reply(plain, 2)
    assert parse_reply(f"Here you go: {plain} Thanks.", 2) == parse_reply(plain, 2)


@pytest.mark.parametrize(
    "text",
    [
        "not json at all",
        "{broken",
        '{"points": "nope"}',
        '{"points": [{"index": 1, "verdict": "maybe"}]}',
        reply("hit"),
        reply("hit", "hit", "hit"),
        '{"points": [{"index": 1, "verdict": "hit"}, {"index": 1, "verdict": "hit"}]}',
    ],
)
def test_parse_rejects_unusable_replies(text: str) -> None:
    with pytest.raises(ValueError):
        parse_reply(text, 2)


async def judge(*replies: str) -> tuple[FakeProvider, Judgment]:
    queue = list(replies)
    provider = FakeProvider(judge_reply=lambda prompt, model: queue.pop(0))
    return provider, await judge_answer(provider, "judge-model", ITEM, "cand")


async def test_score_is_hits_over_points_and_pass_needs_seventy_percent() -> None:
    _, half = await judge(reply("hit", "miss"))
    assert (half.score, half.passed) == (0.5, False)
    _, full = await judge(reply("hit", "hit"))
    assert (full.score, full.passed) == (1.0, True)


async def test_a_wrong_verdict_fails_even_with_a_high_score() -> None:
    item5 = item(2, "haiku", "x").model_copy(update={"rubric": [f"p{i}" for i in range(5)]})
    text = json.dumps(
        {"points": [{"index": i, "verdict": "wrong" if i == 5 else "hit"} for i in range(1, 6)]}
    )
    provider = FakeProvider(judge_reply=lambda prompt, model: text)
    judgment = await judge_answer(provider, "j", item5, "cand")
    assert judgment.score == 0.8
    assert judgment.passed is False


async def test_fenced_reply_is_accepted_on_the_first_try() -> None:
    provider, judgment = await judge(f"```json\n{reply('hit', 'hit')}\n```")
    assert sum(provider.judge_calls.values()) == 1
    assert judgment.score == 1.0


async def test_bad_json_is_retried_once_then_succeeds_and_costs_add_up() -> None:
    provider, judgment = await judge("oops", reply("hit", "hit"))
    assert sum(provider.judge_calls.values()) == 2
    assert judgment.score == 1.0
    assert judgment.cost_usd == pytest.approx(0.002)
    retry_prompt = [p for p, _ in provider.judge_calls][1]
    assert "previous reply was not valid" in retry_prompt


async def test_still_bad_after_the_retry_raises_judge_error() -> None:
    provider = FakeProvider(judge_reply=lambda prompt, model: "never json")
    with pytest.raises(JudgeError, match="invalid judge JSON after 2 attempts"):
        await judge_answer(provider, "j", ITEM, "cand")
    assert sum(provider.judge_calls.values()) == 2


async def test_judge_call_uses_the_grader_system_prompt() -> None:
    seen: list[tuple[str | None, bool]] = []

    class Spy(FakeProvider):
        async def answer(self, prompt, model, *, system=None, json_mode=False):
            seen.append((system, json_mode))
            return await super().answer(prompt, model, system=system, json_mode=json_mode)

    await judge_answer(Spy(), "j", ITEM, "cand")
    assert seen == [(JUDGE_SYSTEM_PROMPT, True)]


def test_json_form_of_a_judgment() -> None:
    j = Judgment("m", ((1, "hit", "a"), (2, "miss", "b")), 0.01, 7.0)
    assert j.to_json() == {
        "model": "m",
        "score": 0.5,
        "pass": False,
        "points": [
            {"index": 1, "verdict": "hit", "reason": "a"},
            {"index": 2, "verdict": "miss", "reason": "b"},
        ],
        "cost_usd": 0.01,
        "latency_ms": 7.0,
    }


def test_rejudge_sample_is_deterministic_and_about_the_fraction() -> None:
    pairs = [(f"g-{n:04d}", arm) for n in range(1000) for arm in ("opus", "router")]
    first = [in_rejudge_sample(i, a, 0.1) for i, a in pairs]
    assert first == [in_rejudge_sample(i, a, 0.1) for i, a in pairs]
    assert 0.08 <= sum(first) / len(pairs) <= 0.12
    assert not any(in_rejudge_sample(i, a, 0.0) for i, a in pairs)
    assert all(in_rejudge_sample(i, a, 1.0) for i, a in pairs)
