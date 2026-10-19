import pytest

from smartrouter.decision.features import parse_override
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
