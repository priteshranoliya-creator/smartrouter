"""Runtime mode: the mode the app is running in now, switchable without a restart.

`state.mode` is the source of truth. `state.decisions` and the recorder's service exist only while
the mode is not off, so a switch applies to the next prompt hook.
"""

from __future__ import annotations

from typing import Any

from smartrouter.decision.factory import build_engine
from smartrouter.decision.service import DecisionService
from smartrouter.telemetry.models import Mode


async def switch_mode(state: Any, mode: Mode) -> None:
    """Set the runtime mode. Raises EngineError when decisions are turned on and the engine cannot start."""
    if mode == "off":
        service: DecisionService | None = state.decisions
        state.mode = mode
        state.decisions = None
        state.recorder.decisions = None
        if service is not None:
            await service.aclose()
        return
    if state.decisions is None:
        engine = state.engine or build_engine(state.settings, state.engine_http)
        service = DecisionService(engine, state.settings, state.prices, state.repo)
        state.engine = engine
        state.decisions = service
        state.recorder.decisions = service
    state.decisions.mode = mode
    state.mode = mode
