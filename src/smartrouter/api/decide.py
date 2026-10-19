"""POST /api/decide: what the router would pick for a prompt in a brand-new session. Writes nothing."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict

from smartrouter.decision.engine import EngineError
from smartrouter.decision.features import parse_override
from smartrouter.decision.service import DecisionService

router = APIRouter(prefix="/api")


class DecideBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    prompt: str
    current_model: str | None = None
    override: Literal["auto", "opus", "sonnet", "haiku"] | None = None


@router.post("/decide")
async def decide(body: DecideBody, request: Request) -> Response:
    decisions: DecisionService | None = request.app.state.decisions
    if decisions is None:
        return JSONResponse({"error": "no decision engine: mode is off"}, status_code=503)
    override = body.override or parse_override(body.prompt)
    try:
        outcome = await decisions.preview(body.prompt, body.current_model, override)
    except EngineError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    answers: dict[str, Any] = {
        question: {"choice": answer.choice, "probabilities": dict(answer.probabilities)}
        for question, answer in outcome.decision.answers.items()
    }
    return JSONResponse(
        {
            "engine": outcome.decision.engine,
            "answers": answers,
            "latency_ms": outcome.decision.latency_ms,
            "desired_model": outcome.desired_model,
            "current_model": body.current_model,
            "chosen_model": outcome.chosen_model or body.current_model,
            "rule": outcome.policy.rule,
            "est_switch_tax_usd": outcome.policy.est_switch_tax_usd,
            "est_saving_usd": outcome.policy.est_saving_usd,
            "mode": decisions.mode,
        }
    )
