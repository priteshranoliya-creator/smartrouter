import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from smartrouter.providers.base import Usage
from smartrouter.settings import ROOT
from smartrouter.telemetry.events import EventBus
from smartrouter.telemetry.pricing import PriceBook, cost, load_pricing
from smartrouter.telemetry.recorder import Exchange, Recorder
from smartrouter.telemetry.repo import Repo

USAGE = Usage(input_tokens=2, output_tokens=4, cache_read_tokens=35617, inference_geo="global")
WHEN = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


@pytest.fixture
def prices() -> PriceBook:
    return load_pricing(ROOT / "config/pricing.yaml")


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo.open(tmp_path / "r.db")


def exchange(**overrides: object) -> Exchange:
    fields: dict[str, object] = {
        "started_at": WHEN,
        "method": "POST",
        "path": "/v1/messages",
        "request_headers": {"x-claude-code-session-id": "s1"},
        "request_model": "claude-haiku-5-5",
        "status": 200,
        "usage": USAGE,
        "latency_ms": 12.5,
        "ttft_ms": 3.0,
        "error": None,
    }
    fields.update(overrides)
    return Exchange(**fields)  # type: ignore[arg-type]


def test_row_carries_tokens_cost_and_publishes_an_event(repo: Repo, prices: PriceBook) -> None:
    bus = EventBus()
    with bus.subscribe() as sub:
        row_id = Recorder(repo, prices, bus).record(exchange())
        event = sub._queue.get_nowait()
    (row,) = repo.list_requests("s1")
    assert row.id == row_id
    assert (row.input_tokens, row.output_tokens, row.cache_read_tokens) == (2, 4, 35617)
    assert row.cost_usd == cost(prices, "claude-haiku-5-5", USAGE, WHEN.date())
    assert row.created_at == WHEN.isoformat()
    assert row.counterfactual_cost_usd is None
    assert event is not None
    assert event.type == "request"
    assert event.data["id"] == row_id
    assert event.data["session_id"] == "s1"


@pytest.mark.parametrize(
    ("request_class", "kind"),
    [
        ("main", "main"),
        ("subagent", "subagent"),
        ("workflow", "subagent"),
        ("compaction", "compaction"),
        ("auxiliary", "background"),
        (None, "main"),
    ],
)
def test_kind_follows_the_request_class_header(
    repo: Repo, prices: PriceBook, request_class: str | None, kind: str
) -> None:
    headers = {"x-claude-code-session-id": "s1"}
    if request_class:
        headers["x-claude-code-request-class"] = request_class
    Recorder(repo, prices, EventBus()).record(exchange(request_headers=headers))
    assert repo.list_requests("s1")[0].kind == kind


def test_session_falls_back_to_the_metadata_value(repo: Repo, prices: PriceBook) -> None:
    Recorder(repo, prices, EventBus()).record(exchange(request_headers={}, metadata_session_id="from-body"))
    assert len(repo.list_requests("from-body")) == 1


def test_unpriced_model_costs_zero_and_says_so(repo: Repo, prices: PriceBook) -> None:
    Recorder(repo, prices, EventBus()).record(exchange(request_model="claude-mystery"))
    row = repo.list_requests("s1")[0]
    assert row.cost_usd == 0.0
    assert row.error == "unpriced model claude-mystery"


def test_only_post_messages_is_recorded(repo: Repo, prices: PriceBook, tmp_path: Path) -> None:
    recorder = Recorder(repo, prices, EventBus())
    assert recorder.record(exchange(path="/v1/messages/count_tokens")) is None
    assert recorder.record(exchange(method="GET")) is None
    count = sqlite3.connect(tmp_path / "r.db").execute("select count(*) from requests").fetchone()[0]
    assert count == 0


def test_failed_exchange_keeps_status_and_error_without_tokens(repo: Repo, prices: PriceBook) -> None:
    Recorder(repo, prices, EventBus()).record(exchange(status=400, usage=None, error="bad model"))
    row = repo.list_requests("s1")[0]
    assert (row.status, row.error, row.input_tokens, row.cost_usd) == (400, "bad model", None, 0.0)
