"""Dashboard read APIs, the runtime mode switch and the health check."""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict

from smartrouter.decision.engine import EngineError
from smartrouter.decision.runtime import switch_mode
from smartrouter.eval import report as eval_report
from smartrouter.telemetry import stats
from smartrouter.telemetry.models import Decision, RequestRow, Session
from smartrouter.telemetry.repo import Repo

router = APIRouter()

WindowParam = Literal["15m", "1h", "24h", "7d", "30d", "all"]
MODES = ("off", "shadow", "active")
ACTIVE_UNAVAILABLE = "active mode is not available until T6.2 is approved"
UPSTREAM_PROBE_TTL_S = 30.0
UPSTREAM_PROBE_TIMEOUT_S = 2.0


def _request_json(row: RequestRow) -> dict[str, Any]:
    without, with_router = stats.request_costs(row)
    return {**asdict(row.request), "without_router_usd": without, "with_router_usd": with_router}


def _decision_json(decision: Decision, request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    return {
        "engine": decision.engine,
        "answers": decision.answers,
        "engine_latency_ms": decision.engine_latency_ms,
        "desired_model": decision.desired_model,
        "chosen_model": decision.chosen_model,
        "rule": decision.rule,
        "mode": decision.mode,
        "est_switch_tax_usd": decision.est_switch_tax_usd,
        "est_saving_usd": decision.est_saving_usd,
        "tier_probability": stats.tier_probability(
            decision.answers, settings.router.decision.question, settings.router.decision.tier_map
        ),
    }


def _last_activity(session: Session) -> str:
    return session.last_request_at or session.started_at


@router.get("/api/sessions")
def sessions(request: Request) -> list[dict[str, Any]]:
    repo: Repo = request.app.state.repo
    rows_by_session: dict[str, list[RequestRow]] = defaultdict(list)
    for row in repo.request_rows():
        if row.request.session_id is not None:
            rows_by_session[row.request.session_id].append(row)
    turn_counts = repo.turn_counts()
    out = []
    for session in sorted(repo.list_sessions(), key=_last_activity, reverse=True):
        total = stats.totals(rows_by_session[session.id])
        out.append(
            {
                "id": session.id,
                "project_path": session.project_path,
                "started_at": session.started_at,
                "last_request_at": session.last_request_at,
                "current_model": session.current_model,
                "state": session.state,
                "turns": turn_counts.get(session.id, 0),
                "requests": total.requests,
                "cost_usd": total.cost_usd,
                "without_router_usd": total.without_router_usd,
                "with_router_usd": total.with_router_usd,
                "saving_usd": total.saving_usd,
            }
        )
    return out


@router.get("/api/sessions/{session_id}/turns")
def session_turns(session_id: str, request: Request) -> Response:
    repo: Repo = request.app.state.repo
    if repo.get_session(session_id) is None:
        return JSONResponse({"error": f"unknown session {session_id!r}"}, status_code=404)
    decisions = {d.turn_id: d for d in repo.session_decisions(session_id)}
    rows_by_turn: dict[int, list[RequestRow]] = defaultdict(list)
    for row in repo.request_rows(session_id=session_id):
        if row.request.turn_id is not None:
            rows_by_turn[row.request.turn_id].append(row)
    out = []
    for turn in repo.list_turns(session_id):
        assert turn.id is not None
        decision = decisions.get(turn.id)
        out.append(
            {
                "id": turn.id,
                "seq": turn.seq,
                "prompt": turn.prompt,
                "override": turn.override,
                "created_at": turn.created_at,
                "decision": None if decision is None else _decision_json(decision, request),
                "requests": [_request_json(r) for r in rows_by_turn[turn.id]],
            }
        )
    return JSONResponse(out)


@router.get("/api/stats")
def stats_endpoint(request: Request, window: WindowParam = "all") -> dict[str, Any]:
    repo: Repo = request.app.state.repo
    decision = request.app.state.settings.router.decision
    since = stats.window_since(window, datetime.now(UTC))
    return stats.compute_stats(
        window=window,
        since=since,
        rows=repo.request_rows(since),
        facts=repo.decision_facts(since),
        sessions=repo.count_sessions(since),
        turns=repo.count_turns(since),
        question=decision.question,
        tier_map=decision.tier_map,
    )


@router.get("/api/eval/runs")
def eval_runs(request: Request) -> list[dict[str, Any]]:
    """Newest first. `summary` is the per-arm summary, `arms` the run's arm names."""
    repo: Repo = request.app.state.repo
    out = []
    for run in repo.list_eval_runs():
        assert run.id is not None
        results = repo.list_eval_results(run.id)
        out.append(
            {
                **asdict(run),
                "n_items": len({r.item_id for r in results}),
                "summary": [asdict(s) for s in eval_report.summarize(results)],
            }
        )
    return out


@router.get("/api/eval/runs/{run_id}")
def eval_run(run_id: int, request: Request) -> Response:
    repo: Repo = request.app.state.repo
    run = repo.get_eval_run(run_id)
    if run is None:
        return JSONResponse({"error": f"unknown eval run {run_id}"}, status_code=404)
    tiers = list(request.app.state.settings.router.tiers)
    report = eval_report.run_report(run, repo.list_eval_results(run_id), tiers)
    return JSONResponse(asdict(report))


class ModeBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mode: Literal["off", "shadow", "active"]


@router.get("/api/mode")
def get_mode(request: Request) -> dict[str, str]:
    return {"mode": request.app.state.mode}


@router.put("/api/mode")
async def put_mode(body: ModeBody, request: Request) -> Response:
    if body.mode == "active":
        return JSONResponse({"error": ACTIVE_UNAVAILABLE}, status_code=409)
    try:
        await switch_mode(request.app.state, body.mode)
    except EngineError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    return JSONResponse({"mode": request.app.state.mode})


async def _upstream_status(state: Any) -> Literal["ok", "error"]:
    """Any HTTP response from the upstream counts as reachable. Cached for 30 s."""
    cached: tuple[float, Literal["ok", "error"]] | None = state.upstream_probe
    now = time.monotonic()
    if cached is not None and now - cached[0] < UPSTREAM_PROBE_TTL_S:
        return cached[1]
    result: Literal["ok", "error"]
    try:
        await state.http.head("/", timeout=UPSTREAM_PROBE_TIMEOUT_S)
        result = "ok"
    except httpx.HTTPError:
        result = "error"
    state.upstream_probe = (now, result)
    return result


@router.get("/healthz")
async def healthz(request: Request) -> Response:
    state = request.app.state
    mode: str = state.mode
    engine = "off" if mode == "off" else ("ready" if state.decisions is not None else "error")
    db = "ok" if state.repo.write_probe() else "error"
    upstream = await _upstream_status(state)
    healthy = engine != "error" and db == "ok" and upstream == "ok"
    body = {
        "status": "ok" if healthy else "degraded",
        "mode": mode,
        "engine": engine,
        "db": db,
        "upstream": upstream,
    }
    return JSONResponse(body, status_code=200 if healthy else 503)
