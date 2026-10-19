import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from smartrouter import cli
from smartrouter.eval.dataset import GoldenItem, load_dataset
from smartrouter.eval.providers import (
    EVAL_SYSTEM_PROMPT,
    ClaudeCliProvider,
    EvalError,
    GeminiProvider,
    ProcessResult,
    build_providers,
    render_prompt,
    run_process,
)
from smartrouter.providers.base import Usage
from smartrouter.settings import ROOT, Settings, load_settings
from smartrouter.telemetry.pricing import PriceBook, cost, load_pricing

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CAPTURED = (FIXTURES / "claude_cli_result.json").read_text()
runner = CliRunner()


def today() -> Any:
    return datetime.now(UTC).date()


@pytest.fixture(scope="module")
def book() -> PriceBook:
    return load_pricing(ROOT / "config/pricing.yaml")


class FakeRunner:
    def __init__(self, result: ProcessResult) -> None:
        self.result = result
        self.calls: list[tuple[Sequence[str], str, Mapping[str, str], Path, float]] = []
        self.cwd_existed = False

    async def __call__(
        self, argv: Sequence[str], stdin: str, env: Mapping[str, str], cwd: Path, timeout_s: float
    ) -> ProcessResult:
        self.calls.append((argv, stdin, env, cwd, timeout_s))
        self.cwd_existed = cwd.is_dir()
        return self.result


def ok(stdout: str = CAPTURED) -> FakeRunner:
    return FakeRunner(ProcessResult(0, stdout, ""))


async def test_claude_parses_captured_json_and_prices_with_our_table(book: PriceBook) -> None:
    fake = ok()
    provider = ClaudeCliProvider(book, runner=fake, environ={})
    answer = await provider.answer("why KeyError?", "claude-opus-5-5")

    assert answer.text == "Use d.get('a') or set the key first."
    assert answer.model == "claude-opus-5-5"
    assert answer.usage == Usage(
        input_tokens=2,
        output_tokens=210,
        cache_read_tokens=300,
        cache_write_5m_tokens=0,
        cache_write_1h_tokens=700,
        inference_geo="global",
    )
    assert answer.cost_usd == pytest.approx(cost(book, "claude-opus-5-5", answer.usage, today()))
    # 2*4 + 210*20 + 300*0.2 + 700*8 micro-dollars; not the CLI's total_cost_usd of 0.0142
    assert answer.cost_usd == pytest.approx(0.009868)
    assert answer.latency_ms == 4210


async def test_claude_command_line_stdin_and_cwd(book: PriceBook) -> None:
    fake = ok()
    await ClaudeCliProvider(book, runner=fake, timeout_s=42, environ={}).answer(
        "the prompt", "claude-haiku-5-5"
    )
    argv, stdin, _, cwd, timeout = fake.calls[0]
    assert list(argv) == [
        "claude",
        "-p",
        "--model",
        "claude-haiku-5-5",
        "--system-prompt",
        EVAL_SYSTEM_PROMPT,
        "--tools",
        "",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--output-format",
        "json",
    ]
    assert stdin == "the prompt"
    assert timeout == 42
    assert fake.cwd_existed
    assert not cwd.exists()
    assert cwd != Path.cwd()


async def test_claude_env_loses_api_variables_only(book: PriceBook) -> None:
    fake = ok()
    environ = {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:8787",
        "ANTHROPIC_API_KEY": "sk-x",
        "CLAUDECODE": "1",
        "HOME": "/home/me",
        "PATH": "/usr/bin",
    }
    await ClaudeCliProvider(book, runner=fake, environ=environ).answer("p", "claude-opus-5-5")
    assert dict(fake.calls[0][2]) == {"HOME": "/home/me", "PATH": "/usr/bin"}


async def test_claude_env_defaults_to_process_environment(
    book: PriceBook, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://x")
    monkeypatch.setenv("SMARTROUTER_PROBE", "1")
    fake = ok()
    await ClaudeCliProvider(book, runner=fake).answer("p", "claude-opus-5-5")
    env = fake.calls[0][2]
    assert "ANTHROPIC_BASE_URL" not in env
    assert env["SMARTROUTER_PROBE"] == "1"


def error_body(**fields: object) -> str:
    return json.dumps({**json.loads(CAPTURED), **fields})


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (ProcessResult(2, "", "boom"), "claude exited 2: boom"),
        (ProcessResult(1, '{"result": "x"}', ""), "claude exited 1"),
        (ProcessResult(0, "not json", ""), "invalid JSON"),
        (ProcessResult(0, "[1]", ""), "not an object"),
        (ProcessResult(0, error_body(is_error=True, result="Credit balance too low"), ""), "Credit balance"),
        (ProcessResult(0, json.dumps({"is_error": False, "usage": {}}), ""), "claude reported an error"),
        (ProcessResult(0, error_body(usage=None), ""), "no usage"),
    ],
)
async def test_claude_error_paths(book: PriceBook, result: ProcessResult, message: str) -> None:
    provider = ClaudeCliProvider(book, runner=FakeRunner(result), environ={})
    with pytest.raises(EvalError, match=message):
        await provider.answer("p", "claude-opus-5-5")


async def test_claude_unpriced_model_is_an_eval_error(book: PriceBook) -> None:
    provider = ClaudeCliProvider(book, runner=ok(), environ={})
    with pytest.raises(EvalError, match="no price for model 'claude-mystery'"):
        await provider.answer("p", "claude-mystery")


async def test_run_process_missing_binary_and_timeout(tmp_path: Path) -> None:
    with pytest.raises(EvalError, match="cannot run"):
        await run_process(["/nonexistent/claude"], "", {}, tmp_path, 5)
    with pytest.raises(EvalError, match="timed out"):
        await run_process(["sleep", "5"], "", {"PATH": "/bin:/usr/bin"}, tmp_path, 0.2)


async def test_run_process_captures_output(tmp_path: Path) -> None:
    done = await run_process(["cat"], "hello", {"PATH": "/bin:/usr/bin"}, tmp_path, 5)
    assert done == ProcessResult(0, "hello", "")


class FakeModels:
    def __init__(self, response: object | Exception) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def fake_client(response: object | Exception) -> tuple[SimpleNamespace, FakeModels]:
    models = FakeModels(response)
    return SimpleNamespace(aio=SimpleNamespace(models=models)), models


def gemini_response(text: str | None = "answer", **meta: int | None) -> SimpleNamespace:
    return SimpleNamespace(text=text, usage_metadata=SimpleNamespace(**meta))


async def test_gemini_usage_maps_thoughts_and_cached_tokens(book: PriceBook) -> None:
    client, models = fake_client(
        gemini_response(
            prompt_token_count=1000,
            cached_content_token_count=400,
            candidates_token_count=50,
            thoughts_token_count=150,
        )
    )
    answer = await GeminiProvider(book, client).answer("the prompt", "gemini-3.1-pro-preview")

    assert answer.usage == Usage(input_tokens=600, output_tokens=200, cache_read_tokens=400)
    assert answer.cost_usd == pytest.approx(cost(book, "gemini-3.1-pro-preview", answer.usage, today()))
    # 600*2 + 400*0.2 + 200*12 micro-dollars
    assert answer.cost_usd == pytest.approx(0.00368)
    assert answer.text == "answer"
    assert answer.model == "gemini-3.1-pro-preview"
    call = models.calls[0]
    assert call["model"] == "gemini-3.1-pro-preview"
    assert call["contents"] == "the prompt"
    assert call["config"].system_instruction == EVAL_SYSTEM_PROMPT


async def test_gemini_missing_counts_are_zero(book: PriceBook) -> None:
    client, _ = fake_client(
        gemini_response(
            prompt_token_count=10,
            cached_content_token_count=None,
            candidates_token_count=5,
            thoughts_token_count=None,
        )
    )
    answer = await GeminiProvider(book, client).answer("p", "gemini-3.8-flash")
    assert answer.usage == Usage(input_tokens=10, output_tokens=5)


async def test_gemini_error_paths(book: PriceBook) -> None:
    with pytest.raises(EvalError, match="GEMINI_API_KEY"):
        await GeminiProvider(book, None).answer("p", "gemini-3.8-flash")
    client, _ = fake_client(RuntimeError("quota"))
    with pytest.raises(EvalError, match="RuntimeError: quota"):
        await GeminiProvider(book, client).answer("p", "gemini-3.8-flash")
    client, _ = fake_client(gemini_response(text=None, prompt_token_count=1))
    with pytest.raises(EvalError, match="no text"):
        await GeminiProvider(book, client).answer("p", "gemini-3.8-flash")


@pytest.fixture
def settings() -> Settings:
    return load_settings(env={"GEMINI_API_KEY": "test-key"})


def test_build_providers_maps_arms_by_pricing_provider(settings: Settings, book: PriceBook) -> None:
    arms = build_providers(settings, book)
    assert list(arms) == list(settings.router.eval.arms)
    by_provider = {arm: book.models[model].provider for arm, model in settings.router.eval.arms.items()}
    assert {"anthropic", "gemini"} <= set(by_provider.values())
    expected = {"anthropic": ClaudeCliProvider, "gemini": GeminiProvider}
    for arm, (provider, _) in arms.items():
        assert isinstance(provider, expected[by_provider[arm]])
    assert {arm: model for arm, (_, model) in arms.items()} == settings.router.eval.arms
    claude_arms = [p for p, _ in arms.values() if isinstance(p, ClaudeCliProvider)]
    assert all(p is claude_arms[0] for p in claude_arms)


def test_build_providers_without_key_defers_the_error(book: PriceBook) -> None:
    settings = load_settings(env={})
    arms = build_providers(settings, book)
    gemini = [p for p, _ in arms.values() if isinstance(p, GeminiProvider)]
    assert gemini


def test_build_providers_rejects_unpriced_arm(settings: Settings, book: PriceBook) -> None:
    eval_config = settings.router.eval.model_copy(update={"arms": {"x": "claude-mystery"}})
    router = settings.router.model_copy(update={"eval": eval_config})
    with pytest.raises(EvalError, match="arm 'x': no price"):
        build_providers(Settings(router, settings.questions, settings.secrets), book)


def golden(**overrides: object) -> GoldenItem:
    base: dict[str, object] = {
        "id": "g-0001",
        "prompt": "Fix it.",
        "context": "",
        "reference_answer": "a",
        "rubric": ["one", "two"],
        "expected_tier": "haiku",
        "task_type": "edit",
        "split": "train",
        "tags": [],
    }
    return GoldenItem.model_validate(base | overrides)


def test_render_prompt() -> None:
    assert render_prompt(golden()) == "Fix it."
    assert render_prompt(golden(context="x = 1")) == "```\nx = 1\n```\n\nFix it."
    nested = render_prompt(golden(context="a\n```\nb"))
    assert nested.startswith("````\na\n```\nb\n````\n\n")


class StubProvider:
    def __init__(self, text: str, fail: bool = False) -> None:
        self.text, self.fail = text, fail

    async def answer(self, prompt: str, model: str) -> Any:
        from smartrouter.eval.providers import EvalAnswer

        if self.fail:
            raise EvalError("down")
        return EvalAnswer(self.text, model, Usage(input_tokens=3, output_tokens=4), 0.000123, 250.0)


@pytest.fixture
def smoke_env(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    arms = {
        "haiku": (StubProvider("line one\nline two " + "x" * 200), "claude-haiku-5-5"),
        "gemini-pro": (StubProvider("", fail=True), "gemini-3.1-pro-preview"),
    }
    monkeypatch.setattr("smartrouter.eval.providers.build_providers", lambda s, b: arms)


def smoke(*args: str) -> Any:
    return runner.invoke(cli.app, ["eval", "smoke", "--dataset", str(FIXTURES / "golden_small.jsonl"), *args])


def test_cli_smoke_prints_each_arm(smoke_env: None) -> None:
    result = smoke("--item", "g-0001")
    assert result.exit_code == 1
    rows = result.output.splitlines()
    assert rows[0].startswith(
        "haiku\tclaude-haiku-5-5\tin=3 cache_read=0 cache_write=0 out=4\t$0.000123\t250ms\t"
    )
    assert rows[0].endswith("line one line two " + "x" * (120 - len("line one line two ")))
    assert rows[1] == "gemini-pro\tgemini-3.1-pro-preview\terror: down"


def test_cli_smoke_arm_filter_and_exit_code(smoke_env: None) -> None:
    result = smoke("--item", "g-0001", "--arms", "haiku")
    assert result.exit_code == 0
    assert "gemini-pro" not in result.output


def test_cli_smoke_unknown_item_and_arm(smoke_env: None) -> None:
    assert smoke("--item", "g-9999").exit_code == 2
    result = smoke("--item", "g-0001", "--arms", "nope")
    assert result.exit_code == 2
    assert "unknown arm(s) nope" in result.output


def test_fixture_dataset_still_loads_for_smoke() -> None:
    assert load_dataset(FIXTURES / "golden_small.jsonl", ["haiku", "sonnet", "opus"])
