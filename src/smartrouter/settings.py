"""Typed settings: config/router.yaml, config/questions.yaml and the environment, parsed once."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from dotenv import dotenv_values
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

Probability = Annotated[float, Field(ge=0, le=1)]
HttpUrl = Annotated[str, StringConstraints(pattern=r"^https?://\S+$")]
NonEmpty = Annotated[str, StringConstraints(min_length=1)]


class Mode(StrEnum):
    OFF = "off"
    SHADOW = "shadow"
    ACTIVE = "active"


class Engine(StrEnum):
    SCALEDOWN = "scaledown"
    LAYA = "laya"
    FAKE = "fake"


class Device(StrEnum):
    CPU = "cpu"
    MPS = "mps"
    CUDA = "cuda"


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ServerConfig(_Frozen):
    host: str
    port: Annotated[int, Field(ge=1, le=65535)]

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, host: str) -> str:
        if host not in LOOPBACK_HOSTS:
            raise ValueError(f"must be loopback ({', '.join(sorted(LOOPBACK_HOSTS))}), got {host!r}")
        return host


class UpstreamConfig(_Frozen):
    anthropic_url: HttpUrl


class PathsConfig(_Frozen):
    db: Path
    calibration: Path
    reports: Path


class TierConfig(_Frozen):
    model: NonEmpty
    provider: Literal["anthropic"]
    strength: Annotated[int, Field(gt=0)]


class ScaledownConfig(_Frozen):
    url: HttpUrl
    timeout_s: Annotated[float, Field(gt=0)]


class LayaConfig(_Frozen):
    checkpoint: NonEmpty
    device: Device


class EnginesConfig(_Frozen):
    scaledown: ScaledownConfig
    laya: LayaConfig


class DecisionConfig(_Frozen):
    engine: Engine
    question: NonEmpty
    tier_map: Annotated[dict[NonEmpty, NonEmpty], Field(min_length=1)]
    deadline_ms: Annotated[int, Field(gt=0)]
    engines: EnginesConfig


class PolicyConfig(_Frozen):
    min_confidence: Probability
    upgrade_confidence: Probability
    min_turns_between_switches: Annotated[int, Field(ge=0)]
    margin: Annotated[float, Field(ge=0)]
    cache_ttl_s: Annotated[int, Field(gt=0)]
    expected_turns_default: Annotated[int, Field(gt=0)]
    expected_turns_min_sessions: Annotated[int, Field(ge=0)]


class EvalConfig(_Frozen):
    judge: NonEmpty
    rejudge_arm: NonEmpty
    rejudge_fraction: Probability
    arms: Annotated[dict[NonEmpty, NonEmpty], Field(min_length=1)]
    router_fallback_tier: NonEmpty = "sonnet"  # tier the router arm serves when the policy abstains


class RouterConfig(_Frozen):
    mode: Mode
    server: ServerConfig
    upstream: UpstreamConfig
    paths: PathsConfig
    tiers: Annotated[dict[NonEmpty, TierConfig], Field(min_length=1)]
    decision: DecisionConfig
    policy: PolicyConfig
    eval: EvalConfig


class Question(_Frozen):
    type: Literal["choice"]
    instructions: NonEmpty
    criteria: Annotated[dict[NonEmpty, NonEmpty], Field(min_length=1)]


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", extra="ignore", frozen=True, env_ignore_empty=True
    )

    scaledown_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    router: RouterConfig
    questions: dict[str, Question]
    secrets: Secrets

    def path(self, name: Literal["db", "calibration", "reports"]) -> Path:
        paths = self.router.paths
        relative = {"db": paths.db, "calibration": paths.calibration, "reports": paths.reports}[name]
        return ROOT / relative


Issue = tuple[str, str]


def tier_issues(router: RouterConfig) -> list[Issue]:
    issues: list[Issue] = []
    seen: dict[int, str] = {}
    for name, tier in router.tiers.items():
        if tier.strength in seen:
            issues.append(
                (
                    f"tiers.{name}.strength",
                    f"duplicate strength {tier.strength} (also {seen[tier.strength]!r})",
                )
            )
        else:
            seen[tier.strength] = name
    known = ", ".join(sorted(router.tiers))
    for label, tier_name in router.decision.tier_map.items():
        if tier_name not in router.tiers:
            issues.append((f"decision.tier_map.{label}", f"unknown tier {tier_name!r} (known: {known})"))
    fallback = router.eval.router_fallback_tier
    if fallback not in router.tiers:
        issues.append(("eval.router_fallback_tier", f"unknown tier {fallback!r} (known: {known})"))
    return issues


def arm_issues(router: RouterConfig) -> list[Issue]:
    known = ", ".join(sorted(router.eval.arms))
    return [
        (f"eval.{field}", f"unknown arm {getattr(router.eval, field)!r} (known: {known})")
        for field in ("judge", "rejudge_arm")
        if getattr(router.eval, field) not in router.eval.arms
    ]


def question_issues(router: RouterConfig, questions: Mapping[str, Question]) -> list[Issue]:
    name = router.decision.question
    question = questions.get(name)
    if question is None:
        return [("decision.question", f"unknown question {name!r} (known: {', '.join(sorted(questions))})")]
    labels = set(router.decision.tier_map)
    expected = set(question.criteria)
    issues: list[Issue] = []
    if missing := sorted(expected - labels):
        issues.append(("decision.tier_map", f"missing labels for question {name!r}: {', '.join(missing)}"))
    if extra := sorted(labels - expected):
        issues.append(("decision.tier_map", f"labels not in question {name!r}: {', '.join(extra)}"))
    return issues


def _format(label: str, issues: list[Issue]) -> str:
    return "\n".join(f"{label}: {path}: {msg}" if path else f"{label}: {msg}" for path, msg in issues)


def _validation_issues(exc: ValidationError) -> list[Issue]:
    return [
        (".".join(str(part) for part in err["loc"]), err["msg"].removeprefix("Value error, "))
        for err in exc.errors()
    ]


def _read_mapping(path: Path, label: str) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"{label}: file not found") from None
    except OSError as exc:
        raise ConfigError(f"{label}: cannot read file ({exc.strerror})") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        detail = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        raise ConfigError(f"{label}: unparseable YAML ({detail})") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{label}: top level must be a mapping, got {type(data).__name__}")
    return data


def _apply_env(raw: dict[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    out = dict(raw)
    if mode := env.get("ROUTER_MODE"):
        out["mode"] = mode
    if port := env.get("ROUTER_PORT"):
        server = out.get("server")
        if server is None:
            out["server"] = {"port": port}
        elif isinstance(server, dict):
            out["server"] = {**server, "port": port}
    return out


def _parse_router(raw: dict[str, Any], env: Mapping[str, str], label: str) -> RouterConfig:
    try:
        return RouterConfig.model_validate(_apply_env(raw, env))
    except ValidationError as exc:
        sources = {"mode": "ROUTER_MODE", "server.port": "ROUTER_PORT"}
        lines = []
        for path, msg in _validation_issues(exc):
            var = sources.get(path)
            source = f"env {var}" if var and env.get(var) else label
            lines.append(_format(source, [(path, msg)]))
        raise ConfigError("\n".join(lines)) from None


def _parse_questions(raw: dict[str, Any], label: str) -> dict[str, Question]:
    try:
        return TypeAdapter(dict[str, Question]).validate_python(raw)
    except ValidationError as exc:
        raise ConfigError(_format(label, _validation_issues(exc))) from None


def _environment() -> dict[str, str]:
    dotenv = {k: v for k, v in dotenv_values(ROOT / ".env").items() if v}
    return {**dotenv, **os.environ}


def load_settings(config_dir: Path = ROOT / "config", env: Mapping[str, str] | None = None) -> Settings:
    env = _environment() if env is None else env
    router_label = f"{config_dir.name}/router.yaml"
    questions_label = f"{config_dir.name}/questions.yaml"

    router = _parse_router(_read_mapping(config_dir / "router.yaml", router_label), env, router_label)
    questions = _parse_questions(
        _read_mapping(config_dir / "questions.yaml", questions_label), questions_label
    )

    issues = tier_issues(router) + arm_issues(router) + question_issues(router, questions)
    if issues:
        raise ConfigError(_format(router_label, issues))

    secrets = Secrets(
        _env_file=None,
        scaledown_api_key=env.get("SCALEDOWN_API_KEY") or None,
        gemini_api_key=env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY") or None,
    )
    return Settings(router=router, questions=questions, secrets=secrets)
