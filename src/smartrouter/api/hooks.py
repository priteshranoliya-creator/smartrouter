"""Claude Code hook endpoints. Each handler finishes its writes before it responds."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from smartrouter.decision.features import parse_override
from smartrouter.telemetry.events import Event, EventBus
from smartrouter.telemetry.models import Compaction, Session, Turn
from smartrouter.telemetry.repo import Repo

router = APIRouter(prefix="/hooks")


class PromptPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    session_id: str = Field(min_length=1)
    prompt: str
    cwd: str | None = None
    transcript_path: str | None = None
    prompt_id: str | None = None


class PrecompactPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    session_id: str = Field(min_length=1)
    trigger: Literal["manual", "auto"]


def _bad_request(exc: ValueError) -> Response:
    message = (
        "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors(include_url=False))
        if isinstance(exc, ValidationError)
        else "body is not valid JSON"
    )
    return JSONResponse({"error": f"invalid payload: {message}"}, status_code=400)


def _now() -> str:
    return datetime.now(UTC).isoformat()


@router.post("/prompt")
async def prompt_submitted(request: Request) -> Response:
    try:
        payload = PromptPayload.model_validate_json(await request.body())
    except ValueError as exc:
        return _bad_request(exc)
    repo: Repo = request.app.state.repo
    bus: EventBus = request.app.state.bus
    now = _now()
    repo.ensure_session(Session(id=payload.session_id, project_path=payload.cwd or "", started_at=now))
    turn = Turn(
        session_id=payload.session_id,
        seq=repo.next_turn_seq(payload.session_id),
        prompt_id=payload.prompt_id,
        prompt=payload.prompt,
        override=parse_override(payload.prompt),
        created_at=now,
    )
    turn = replace(turn, id=repo.insert_turn(turn))
    bus.publish(Event("turn", asdict(turn)))
    return JSONResponse({"turn_id": turn.id}, status_code=202)


@router.post("/precompact")
async def precompacting(request: Request) -> Response:
    try:
        payload = PrecompactPayload.model_validate_json(await request.body())
    except ValueError as exc:
        return _bad_request(exc)
    repo: Repo = request.app.state.repo
    bus: EventBus = request.app.state.bus
    now = _now()
    repo.ensure_session(Session(id=payload.session_id, project_path="", started_at=now))
    session = repo.get_session(payload.session_id)
    assert session is not None
    warm = (
        None if session.current_model is None else repo.get_session_model(session.id, session.current_model)
    )
    repo.set_session_state(session.id, "compacting")
    compaction = Compaction(
        session_id=session.id,
        trigger=payload.trigger,
        prefix_tokens_before=None if warm is None else warm.prefix_tokens,
        created_at=now,
    )
    compaction = replace(compaction, id=repo.insert_compaction(compaction))
    bus.publish(Event("compaction", asdict(compaction)))
    return JSONResponse({"compaction_id": compaction.id}, status_code=202)
