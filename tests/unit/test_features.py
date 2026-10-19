import pytest

from smartrouter.decision.engine import DecisionState
from smartrouter.decision.features import (
    MIN_KEEP_CHARS,
    SessionView,
    build_state,
    estimate_tokens,
    parse_override,
)
from smartrouter.telemetry.models import Override


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("@opus design this", "opus"),
        ("@sonnet fix it", "sonnet"),
        ("@haiku fix it", "haiku"),
        ("@auto", "auto"),
        ("  \n @Haiku\tfix it", "haiku"),
        ("@OPUS", "opus"),
        ("@opus\nmultiline", "opus"),
        ("@opusx fix it", None),
        ("@opus-4 fix it", None),
        ("email@opus fix it", None),
        ("please use @opus", None),
        ("@gpt fix it", None),
        ("@", None),
        ("", None),
    ],
)
def test_parse_override(prompt: str, expected: Override | None) -> None:
    assert parse_override(prompt) == expected


SESSION = SessionView(
    turn_seq=3,
    current_tier="sonnet",
    minutes_since_last_request=12.0,
    just_compacted=True,
    previous_prompt="fix the flaky test\nin  tests/test_x.py",
)


def test_state_without_session_has_no_facts() -> None:
    state = build_state("rename foo to bar", None)
    assert state.prompt == "rename foo to bar"
    assert state.facts == ()
    assert state.render() == "rename foo to bar"


def test_override_prefix_is_stripped() -> None:
    assert build_state("@opus design this", None).prompt == "design this"
    assert build_state("  @Haiku\n\nfix it", None).prompt == "fix it"
    assert build_state("@opusx fix it", None).prompt == "@opusx fix it"


def test_five_facts_each_one_short_line() -> None:
    state = build_state("go", SESSION)
    assert state.facts == (
        "turn 3 of this session",
        "current model tier: sonnet",
        "idle 12 minutes",
        "conversation was just compacted",
        "previous request: fix the flaky test in tests/test_x.py",
    )
    assert all("\n" not in fact for fact in state.facts)


def test_previous_request_fact_is_capped_at_160_chars() -> None:
    session = SessionView(1, None, None, False, "x" * 500)
    assert build_state("go", session, budget_tokens=10_000).facts[-1] == "previous request: " + "x" * 160


def test_absent_session_details_add_no_facts() -> None:
    session = SessionView(1, None, 0.2, False, None)
    assert build_state("go", session).facts == ("turn 1 of this session",)


def test_prompt_at_2400_chars_is_untouched_and_at_2401_keeps_head_and_tail() -> None:
    at_limit = "a" * 1200 + "b" * 1200
    assert build_state(at_limit, None, budget_tokens=10_000).prompt == at_limit
    over = "a" * 1200 + "m" + "b" * 1200
    state = build_state(over, None, budget_tokens=10_000)
    assert state.prompt == "a" * 1200 + "\n...\n" + "b" * 1200


def test_prompt_shrinks_before_any_fact_is_dropped() -> None:
    prompt = "p" * 600
    full = build_state(prompt, SESSION, budget_tokens=10_000)
    tight = estimate_tokens(DecisionState("p" * 450, full.facts).render())  # room above the 2x200 floor
    state = build_state(prompt, SESSION, budget_tokens=tight)
    assert state.facts == full.facts
    assert len(state.prompt) < len(prompt)


def test_facts_are_dropped_from_the_end_once_the_prompt_is_at_its_floor() -> None:
    prompt = "p" * 600
    full = build_state(prompt, SESSION, budget_tokens=10_000)
    floor = DecisionState("p" * MIN_KEEP_CHARS + "\n...\n" + "p" * MIN_KEEP_CHARS, full.facts[:2])
    state = build_state(prompt, SESSION, budget_tokens=estimate_tokens(floor.render()))
    assert state.facts == full.facts[:2]


@pytest.mark.parametrize(
    "prompt",
    [
        "HEAD " + "the quick brown fox jumps over the lazy dog " * 1200 + " TAIL",
        "शुरू " + "यह एक हिंदी वाक्य है जो बहुत लंबा है " * 280 + " अंत",
        "😀start" + "🔥🚀✨🙂" * 3000 + "end😀",
    ],
    ids=["english-50k", "hindi-10k", "emoji"],
)
def test_budget_is_enforced_and_head_and_tail_survive(prompt: str) -> None:
    state = build_state(prompt, SESSION)
    assert estimate_tokens(state.render()) <= 320
    assert state.prompt.startswith(prompt[:5])
    assert state.prompt.endswith(prompt[-3:])
    assert "\n...\n" in state.prompt


def test_budget_shrink_is_symmetric_and_maximal() -> None:
    prompt = "a" * 5000 + "b" * 5000
    state = build_state(prompt, None, budget_tokens=100)
    head, tail = state.prompt.split("\n...\n")
    assert len(head) == len(tail)
    assert estimate_tokens(state.render()) <= 100
    wider = DecisionState("a" * (len(head) + 1) + "\n...\n" + "b" * (len(tail) + 1))
    assert estimate_tokens(wider.render()) > 100


def test_estimate_tokens_counts_utf8_bytes_over_three() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abc") == 1
    assert estimate_tokens("abcd") == 2
    assert estimate_tokens("हि") == 2


def test_long_prompt_keeps_its_session_facts() -> None:
    session = SessionView(
        turn_seq=4,
        current_tier="sonnet",
        minutes_since_last_request=3,
        just_compacted=False,
        previous_prompt="add pagination to the orders endpoint",
    )
    state = build_state("refactor the retry loop " * 400, session, budget_tokens=320)
    assert estimate_tokens(state.render()) <= 320
    assert any(f.startswith("previous request: add pagination") for f in state.facts)
    assert len(state.prompt) >= 2 * MIN_KEEP_CHARS
