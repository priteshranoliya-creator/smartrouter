from __future__ import annotations

import copy
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from smartrouter.settings import ROOT, ConfigError, Mode, load_settings

REAL_CONFIG = ROOT / "config"
DELETE = object()

BASE: dict[str, Any] = {
    "mode": "shadow",
    "server": {"host": "127.0.0.1", "port": 8787},
    "upstream": {"anthropic_url": "https://api.anthropic.com"},
    "paths": {"db": "data/router.db", "calibration": "data/calibration.json", "reports": "data/reports"},
    "tiers": {
        "haiku": {"model": "m-haiku", "provider": "anthropic", "strength": 1},
        "sonnet": {"model": "m-sonnet", "provider": "anthropic", "strength": 2},
        "opus": {"model": "m-opus", "provider": "anthropic", "strength": 3},
    },
    "decision": {
        "engine": "scaledown",
        "question": "complexity",
        "tier_map": {"A": "haiku", "B": "sonnet", "C": "sonnet", "D": "opus"},
        "deadline_ms": 2500,
        "engines": {
            "scaledown": {"url": "https://scaledown.example/classify", "timeout_s": 5},
            "laya": {"checkpoint": "english", "device": "cpu"},
        },
    },
    "policy": {
        "min_confidence": 0.6,
        "upgrade_confidence": 0.75,
        "min_turns_between_switches": 3,
        "margin": 0.2,
        "cache_ttl_s": 3600,
        "expected_turns_default": 8,
        "expected_turns_min_sessions": 20,
    },
    "eval": {
        "judge": "gemini-pro",
        "rejudge_arm": "opus",
        "rejudge_fraction": 0.1,
        "arms": {"opus": "m-opus", "gemini-pro": "m-gemini"},
    },
}


def mutated(base: dict[str, Any], dotted: str, value: object) -> dict[str, Any]:
    data = copy.deepcopy(base)
    *parents, leaf = dotted.split(".")
    node = data
    for key in parents:
        node = node[key]
    if value is DELETE:
        del node[leaf]
    else:
        node[leaf] = value
    return data


def write_config(tmp_path: Path, router: Any = BASE, questions: Any = None) -> Path:
    config = tmp_path / "config"
    config.mkdir()
    (config / "router.yaml").write_text(
        yaml.safe_dump(router, sort_keys=False) if not isinstance(router, str) else router
    )
    if questions is None:
        shutil.copy(REAL_CONFIG / "questions.yaml", config / "questions.yaml")
    else:
        (config / "questions.yaml").write_text(
            yaml.safe_dump(questions, sort_keys=False) if not isinstance(questions, str) else questions
        )
    return config


def real_questions() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((REAL_CONFIG / "questions.yaml").read_text())
    return loaded


ROUTER_CASES = [
    ("mode", "bogus", "mode"),
    ("server.host", "0.0.0.0", "server.host"),
    ("server.host", "192.168.1.5", "loopback"),
    ("server.port", 0, "server.port"),
    ("server.port", 65536, "server.port"),
    ("policy.min_confidence", 1.5, "policy.min_confidence"),
    ("policy.upgrade_confidence", -0.1, "policy.upgrade_confidence"),
    ("eval.rejudge_fraction", 2, "eval.rejudge_fraction"),
    ("policy.margin", -1, "policy.margin"),
    ("decision.deadline_ms", 0, "decision.deadline_ms"),
    ("policy.cache_ttl_s", 0, "policy.cache_ttl_s"),
    ("policy.expected_turns_default", 0, "policy.expected_turns_default"),
    ("decision.engines.scaledown.timeout_s", 0, "decision.engines.scaledown.timeout_s"),
    ("decision.engines.laya.device", "tpu", "decision.engines.laya.device"),
    ("decision.engine", "bogus", "decision.engine"),
    ("tiers.opus.strength", 2, "tiers.opus.strength"),
    ("tiers.opus.strength", 0, "tiers.opus.strength"),
    ("decision.tier_map.D", "gpt", "decision.tier_map.D: unknown tier 'gpt' (known: haiku, opus, sonnet)"),
    ("decision.question", "nope", "decision.question: unknown question 'nope'"),
    ("decision.tier_map.D", DELETE, "missing labels for question 'complexity': D"),
    ("decision.tier_map.Z", "haiku", "labels not in question 'complexity': Z"),
    ("eval.judge", "nope", "eval.judge: unknown arm 'nope'"),
    ("eval.rejudge_arm", "nope", "eval.rejudge_arm: unknown arm 'nope'"),
    ("policy.min_confidance", 0.5, "policy.min_confidance"),
    ("servr", {"port": 1}, "servr"),
    ("server", DELETE, "server"),
]


@pytest.mark.parametrize(("field", "value", "expected"), ROUTER_CASES)
def test_invalid_router_yaml_fails_with_field_path(
    tmp_path: Path, field: str, value: object, expected: str
) -> None:
    config = write_config(tmp_path, mutated(BASE, field, value))
    with pytest.raises(ConfigError) as exc:
        load_settings(config, env={})
    message = str(exc.value)
    assert "config/router.yaml" in message
    assert expected in message


QUESTION_CASES = [
    ("complexity.type", "score", "complexity.type"),
    ("complexity.type", "noul", "complexity.type"),
    ("complexity.criteria", {}, "complexity.criteria"),
    ("complexity.instructions", DELETE, "complexity.instructions"),
    ("complexity.extra", 1, "complexity.extra"),
]


@pytest.mark.parametrize(("field", "value", "expected"), QUESTION_CASES)
def test_invalid_questions_yaml_fails_with_field_path(
    tmp_path: Path, field: str, value: object, expected: str
) -> None:
    config = write_config(tmp_path, questions=mutated(real_questions(), field, value))
    with pytest.raises(ConfigError) as exc:
        load_settings(config, env={})
    message = str(exc.value)
    assert "config/questions.yaml" in message
    assert expected in message


FILE_CASES: list[tuple[str, Callable[[Path], None], str]] = [
    ("router missing", lambda c: (c / "router.yaml").unlink(), "router.yaml: file not found"),
    ("questions missing", lambda c: (c / "questions.yaml").unlink(), "questions.yaml: file not found"),
    (
        "router unparseable",
        lambda c: (c / "router.yaml").write_text("mode: [unclosed"),
        "router.yaml: unparseable YAML",
    ),
    (
        "questions unparseable",
        lambda c: (c / "questions.yaml").write_text("a: [unclosed"),
        "questions.yaml: unparseable YAML",
    ),
    (
        "router not a mapping",
        lambda c: (c / "router.yaml").write_text("- a\n- b\n"),
        "router.yaml: top level must be a mapping",
    ),
    (
        "questions empty",
        lambda c: (c / "questions.yaml").write_text(""),
        "questions.yaml: top level must be a mapping",
    ),
]


@pytest.mark.parametrize(("name", "break_it", "expected"), FILE_CASES, ids=[c[0] for c in FILE_CASES])
def test_broken_files_fail_with_file_name(
    tmp_path: Path, name: str, break_it: Callable[[Path], None], expected: str
) -> None:
    config = write_config(tmp_path)
    break_it(config)
    with pytest.raises(ConfigError, match=expected):
        load_settings(config, env={})


def test_all_errors_are_reported_one_per_line(tmp_path: Path) -> None:
    router = mutated(mutated(BASE, "server.port", 0), "policy.margin", -1)
    config = write_config(tmp_path, router)
    with pytest.raises(ConfigError) as exc:
        load_settings(config, env={})
    lines = str(exc.value).splitlines()
    assert len(lines) == 2
    assert any("server.port" in line for line in lines)
    assert any("policy.margin" in line for line in lines)


@pytest.mark.parametrize(
    ("env", "var", "path"),
    [
        ({"ROUTER_MODE": "bogus"}, "ROUTER_MODE", "mode"),
        ({"ROUTER_PORT": "abc"}, "ROUTER_PORT", "server.port"),
        ({"ROUTER_PORT": "99999"}, "ROUTER_PORT", "server.port"),
    ],
)
def test_invalid_env_override_fails_naming_the_variable(
    tmp_path: Path, env: dict[str, str], var: str, path: str
) -> None:
    config = write_config(tmp_path)
    with pytest.raises(ConfigError) as exc:
        load_settings(config, env=env)
    assert f"env {var}: {path}" in str(exc.value)


def test_real_repo_config_loads() -> None:
    settings = load_settings(env={})
    assert settings.router.mode is Mode.SHADOW
    assert settings.router.server.host == "127.0.0.1"
    assert set(settings.router.decision.tier_map) == set(
        settings.questions[settings.router.decision.question].criteria
    )
    assert settings.path("db") == ROOT / "data" / "router.db"
    assert settings.secrets.scaledown_api_key is None


def test_env_overrides_mode_and_port(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    settings = load_settings(config, env={"ROUTER_MODE": "active", "ROUTER_PORT": "9001"})
    assert settings.router.mode is Mode.ACTIVE
    assert settings.router.server.port == 9001


def test_empty_env_values_do_not_override(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    settings = load_settings(config, env={"ROUTER_MODE": "", "ROUTER_PORT": ""})
    assert settings.router.mode is Mode.SHADOW
    assert settings.router.server.port == 8787


def test_secrets_come_from_env_and_never_print(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    settings = load_settings(
        config, env={"SCALEDOWN_API_KEY": "sd-secret-value", "GEMINI_API_KEY": "gm-secret-value"}
    )
    assert settings.secrets.scaledown_api_key is not None
    assert settings.secrets.scaledown_api_key.get_secret_value() == "sd-secret-value"
    assert settings.secrets.gemini_api_key is not None
    assert settings.secrets.gemini_api_key.get_secret_value() == "gm-secret-value"
    shown = repr(settings) + str(settings.secrets) + repr(settings.secrets)
    assert "sd-secret-value" not in shown
    assert "gm-secret-value" not in shown


def test_unset_or_empty_secrets_are_none(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    settings = load_settings(config, env={"SCALEDOWN_API_KEY": ""})
    assert settings.secrets.scaledown_api_key is None
    assert settings.secrets.gemini_api_key is None


def test_settings_are_frozen(tmp_path: Path) -> None:
    settings = load_settings(write_config(tmp_path), env={})
    with pytest.raises(ValueError):
        settings.router.server.port = 1  # type: ignore[misc]
