"""Eval providers: answer one single-turn prompt with a model and price the usage from pricing.yaml."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol

from google import genai
from google.genai import types

from smartrouter.eval.dataset import GoldenItem
from smartrouter.providers.anthropic import usage_from_dict
from smartrouter.providers.base import Usage
from smartrouter.settings import Settings
from smartrouter.telemetry.pricing import PriceBook, PricingError, cost

EVAL_SYSTEM_PROMPT = (
    "You are an expert software engineer. Answer the request directly and concisely. "
    "You cannot read files or run commands; answer from the information given."
)
DEFAULT_TIMEOUT_S = 300.0
STRIPPED_ENV = ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "CLAUDECODE")


class EvalError(Exception):
    """A model call failed or returned something unusable."""


@dataclass(frozen=True)
class EvalAnswer:
    text: str
    model: str
    usage: Usage
    cost_usd: float
    latency_ms: float


class EvalProvider(Protocol):
    async def answer(self, prompt: str, model: str) -> EvalAnswer: ...


def render_prompt(item: GoldenItem) -> str:
    if not item.context:
        return item.prompt
    fence = "```"
    while fence in item.context:
        fence += "`"
    return f"{fence}\n{item.context}\n{fence}\n\n{item.prompt}"


def _today() -> date:
    return datetime.now(UTC).date()


def _price(book: PriceBook, model: str, usage: Usage) -> float:
    try:
        return cost(book, model, usage, _today())
    except PricingError as exc:
        raise EvalError(str(exc)) from exc


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str], str, Mapping[str, str], Path, float], Awaitable[ProcessResult]]


async def run_process(
    argv: Sequence[str], stdin: str, env: Mapping[str, str], cwd: Path, timeout_s: float
) -> ProcessResult:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(env),
            cwd=cwd,
        )
    except OSError as exc:
        raise EvalError(f"cannot run {argv[0]}: {exc.strerror or exc}") from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin.encode()), timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise EvalError(f"{argv[0]} timed out after {timeout_s:g}s") from None
    return ProcessResult(proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace"))


class ClaudeCliProvider:
    """Answers through `claude -p`, which uses the logged-in Team plan (no API key exists)."""

    def __init__(
        self,
        book: PriceBook,
        *,
        runner: Runner = run_process,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._book = book
        self._runner = runner
        self._timeout_s = timeout_s
        self._environ = environ

    def _env(self) -> dict[str, str]:
        source = os.environ if self._environ is None else self._environ
        return {k: v for k, v in source.items() if k not in STRIPPED_ENV}

    async def answer(self, prompt: str, model: str) -> EvalAnswer:
        argv = [
            "claude",
            "-p",
            "--model",
            model,
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
        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="smartrouter-eval-") as cwd:
            done = await self._runner(argv, prompt, self._env(), Path(cwd), self._timeout_s)
        wall_ms = (time.perf_counter() - started) * 1000
        if done.returncode != 0:
            detail = (done.stderr.strip() or done.stdout.strip())[:300]
            raise EvalError(f"claude exited {done.returncode}: {detail}")
        return self._parse(done.stdout, model, wall_ms)

    def _parse(self, stdout: str, model: str, wall_ms: float) -> EvalAnswer:
        try:
            body = json.loads(stdout)
        except ValueError as exc:
            raise EvalError(f"claude printed invalid JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise EvalError("claude printed JSON that is not an object")
        text = body.get("result")
        if body.get("is_error") or not isinstance(text, str):
            reason = text if isinstance(text, str) else body.get("subtype") or "no result"
            raise EvalError(f"claude reported an error: {reason}")
        usage_raw = body.get("usage")
        if not isinstance(usage_raw, dict):
            raise EvalError("claude result has no usage")
        usage = usage_from_dict(usage_raw)
        api_ms = body.get("duration_api_ms")
        latency = float(api_ms) if isinstance(api_ms, int | float) and api_ms > 0 else wall_ms
        return EvalAnswer(text, model, usage, _price(self._book, model, usage), latency)


class GeminiProvider:
    def __init__(self, book: PriceBook, client: Any | None, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self._book = book
        self._client = client
        self._timeout_s = timeout_s

    async def answer(self, prompt: str, model: str) -> EvalAnswer:
        if self._client is None:
            raise EvalError("GEMINI_API_KEY is not set")
        config = types.GenerateContentConfig(system_instruction=EVAL_SYSTEM_PROMPT)
        started = time.perf_counter()
        try:
            response = await asyncio.wait_for(
                self._client.aio.models.generate_content(model=model, contents=prompt, config=config),
                self._timeout_s,
            )
        except TimeoutError:
            raise EvalError(f"gemini timed out after {self._timeout_s:g}s") from None
        except Exception as exc:
            raise EvalError(f"gemini call failed: {type(exc).__name__}: {exc}") from exc
        latency = (time.perf_counter() - started) * 1000
        text = getattr(response, "text", None)
        if not isinstance(text, str) or not text:
            raise EvalError("gemini returned no text")
        usage = _gemini_usage(getattr(response, "usage_metadata", None))
        return EvalAnswer(text, model, usage, _price(self._book, model, usage), latency)


def _gemini_usage(meta: Any) -> Usage:
    def count(name: str) -> int:
        value = getattr(meta, name, None)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    cached = count("cached_content_token_count")
    return Usage(
        input_tokens=max(count("prompt_token_count") - cached, 0),
        output_tokens=count("candidates_token_count") + count("thoughts_token_count"),
        cache_read_tokens=cached,
    )


Arms = dict[str, tuple[EvalProvider, str]]


def build_providers(settings: Settings, book: PriceBook) -> Arms:
    """Map every configured arm to its provider and model id, by the model's provider in pricing.yaml."""
    key = settings.secrets.gemini_api_key
    claude = ClaudeCliProvider(book)
    gemini = GeminiProvider(book, genai.Client(api_key=key.get_secret_value()) if key else None)
    arms: Arms = {}
    for arm, model in settings.router.eval.arms.items():
        price = book.models.get(model)
        if price is None:
            raise EvalError(f"arm {arm!r}: no price for model {model!r} in pricing.yaml")
        if price.provider == "anthropic":
            arms[arm] = (claude, model)
        elif price.provider == "gemini":
            arms[arm] = (gemini, model)
        else:
            raise EvalError(f"arm {arm!r}: no eval provider for {price.provider!r}")
    return arms
