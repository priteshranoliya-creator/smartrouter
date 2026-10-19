"""Prompt features for the decision engine. Today: the `@model` override prefix."""

from __future__ import annotations

import re

from smartrouter.telemetry.models import Override

_OVERRIDES: dict[str, Override] = {"opus": "opus", "sonnet": "sonnet", "haiku": "haiku", "auto": "auto"}
_OVERRIDE_PREFIX = re.compile(rf"\s*@({'|'.join(_OVERRIDES)})(?=\s|$)", re.IGNORECASE)


def parse_override(prompt: str) -> Override | None:
    """The model the user pinned with a leading `@opus`, `@sonnet`, `@haiku` or `@auto`, else None."""
    match = _OVERRIDE_PREFIX.match(prompt)
    return None if match is None else _OVERRIDES[match.group(1).lower()]
