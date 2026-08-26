"""FastAPI server for the control-plane console.

Endpoints:
  GET  /api/scenarios   the fixed scenarios (with locked prompts)
  GET  /api/units       composable units, grouped-ready metadata (point, composer, verdict)
  POST /api/run         run a scenario with a chosen set of units (ndjson stream)
  POST /api/resume      approve/deny a suspended (approval) call (ndjson stream)

Prompts come from the fixed scenarios only; the client never sends free text, so a public
link cannot be used as an open LLM proxy.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from nexora import AgentRuntime
from nexora.orchestrator import AgentSuspended
from pydantic import BaseModel

from .dormancy import dormant_reason
from .provider import DEFAULT_MODEL, openrouter_model
from .scenarios import SCENARIOS, SYSTEM_PROMPT
from .store import make_store
from .tools import DemoTools
from .units import UNITS, UNITS_BY_NAME, compose_controls

load_dotenv()

STATIC = Path(__file__).parent / "static"
_SCENARIO_BY_ID = {s["id"]: s for s in SCENARIOS}

_sessions: dict[str, dict[str, Any]] = {}
_store: Any = None
_closer: Any = None


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Open the step ledger on startup and close it on shutdown."""
    global _store, _closer
    _store, _closer = await make_store()
    yield
    if _closer is not None:
        await _closer()


app = FastAPI(title="Nexora Control Plane Console", lifespan=lifespan)


class RunRequest(BaseModel):
    """A scenario id plus the control-plane units to compose."""

    scenario_id: str
    units: list[str] = []


class ResumeRequest(BaseModel):
    """An operator decision for a suspended call."""

    run_id: str
    pending_id: str
    approved: bool


async def _capped(turn: int, _text: str, _calls: list[dict[str, Any]]) -> bool:
    """Bounded-turn backstop: runaway/cost guard for a public link, and a terminator for
    units like log_gate that veto completion."""
    return turn >= 8


def _frame(kind: str, **payload: Any) -> str:
    """Encode one newline-delimited stream frame."""
    return json.dumps({"kind": kind, **payload}, ensure_ascii=False, default=str) + "\n"


async def _stream(run_id: str, attempt: Any, *, selected: list[str], scenario_id: str) -> AsyncIterator[str]:
    """Run one attempt, translate events into frames, and end with a policy summary."""
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    fired: dict[str, int] = {}

    def mark(unit: str) -> None:
        fired[unit] = fired.get(unit, 0) + 1

    async def on_event(event: dict[str, Any]) -> None:
        # A refused call's stand-in result carries the unit's verdict — surface it.
        if event.get("type") == "tool_result" and event.get("executed") is False:
            res = event.get("result") or {}
            unit = res.get("unit", "control")
            mark(unit)
            await queue.put(
                {
                    "kind": "unit",
                    "unit": unit,
                    "verdict": "suspend" if res.get("type") == "suspend" else "deny",
                    "message": res.get("message") or res.get("reason") or "denied",
                }
            )
            return
        # An executed result a unit rewrote in place: announce the masking.
        if event.get("type") == "tool_result":
            res = event.get("result") or {}
            if isinstance(res, dict) and res.get("redacted_by"):
                mark(res["redacted_by"])
                await queue.put(
                    {
                        "kind": "unit",
                        "unit": res["redacted_by"],
                        "verdict": "rewrite",
                        "message": "결과의 PII를 마스킹함 (모델·UI엔 원본 미유입)",
                    }
                )
        if event.get("type") in {"text", "thinking", "tool_call", "tool_result"}:
            await queue.put({"kind": "agent", "event": event})

    async def publish(event_type: str, payload: dict[str, Any]) -> None:
        # A before_finish veto injects steering, surfaced as a control-kind context injection.
        if str(event_type).endswith("context_injected") and payload.get("kind") == "control":
            mark("log_gate")
            await queue.put(
                {"kind": "unit", "unit": "log_gate", "verdict": "steer",
                 "message": "종료 거부 — 기록될 때까지 한 라운드 더"}
            )
        await queue.put({"kind": "lifecycle", "type": str(event_type), "payload": payload})

    def summary_frame() -> dict[str, Any]:
        rows = []
        for name in selected:
            n = fired.get(name, 0)
            rows.append(
                {"name": name, "fired": n > 0, "count": n,
                 "reason": None if n > 0 else dormant_reason(name, scenario_id)}
            )
        return {"kind": "policy_summary", "units": rows}

    async def run_attempt() -> None:
        runtime = AgentRuntime(store=_store, emit=publish)
        await queue.put({"kind": "meta", "run_id": run_id})
        try:
            outcome = await attempt(runtime, on_event)
            await queue.put({"kind": "outcome", "outcome": outcome})
            if selected:
                await queue.put(summary_frame())
        except AgentSuspended as stopped:
            await queue.put({"kind": "suspended", "pending_id": stopped.pending_id, "tool_call_id": stopped.tool_call_id})
        except Exception as failure:  # noqa: BLE001 - surface any failure as a frame
            await queue.put({"kind": "error", "message": str(failure)})
        finally:
            await queue.put(None)

    task = asyncio.create_task(run_attempt())
    try:
        while (item := await queue.get()) is not None:
            yield _frame(**item)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@app.get("/api/scenarios")
async def scenarios() -> list[dict[str, Any]]:
    """Return the fixed scenarios."""
    return SCENARIOS


@app.get("/api/units")
async def units() -> dict[str, Any]:
    """Return composable units and the default model, for the composition view."""
    return {
        "model": DEFAULT_MODEL,
        "units": [
            {"name": u.name, "point": u.point, "composer": u.composer,
             "verdict": u.verdict, "title": u.title, "desc": u.desc}
            for u in UNITS
        ],
    }


@app.post("/api/run")
async def run(request: RunRequest) -> StreamingResponse:
    """Start a scenario run with the chosen units."""
    scenario = _SCENARIO_BY_ID.get(request.scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="unknown scenario_id")
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    selected = [u for u in request.units if u in UNITS_BY_NAME]
    _sessions[run_id] = {"units": selected, "tools": DemoTools(), "scenario_id": request.scenario_id}

    async def attempt(runtime: AgentRuntime, on_event: Any) -> dict[str, Any]:
        return await runtime.run(
            run_id, openrouter_model(), _sessions[run_id]["tools"], scenario["prompt"],
            controls=compose_controls(selected), on_event=on_event,
            system_prompt=SYSTEM_PROMPT, should_stop_after_turn=_capped,
        )

    return StreamingResponse(
        _stream(run_id, attempt, selected=selected, scenario_id=request.scenario_id),
        media_type="application/x-ndjson",
    )


@app.post("/api/resume")
async def resume(request: ResumeRequest) -> StreamingResponse:
    """Approve or deny a suspended call and continue the run."""
    session = _sessions.get(request.run_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown run_id")
    answer = (
        {"type": "text", "text": "approved by the human"}
        if request.approved
        else {"type": "error", "message": "denied by the human"}
    )

    async def attempt(runtime: AgentRuntime, on_event: Any) -> dict[str, Any]:
        return await runtime.resume(
            request.run_id, request.pending_id, answer, openrouter_model(), session["tools"],
            controls=compose_controls(session["units"]), on_event=on_event,
            system_prompt=SYSTEM_PROMPT, should_stop_after_turn=_capped,
        )

    return StreamingResponse(
        _stream(request.run_id, attempt, selected=session["units"], scenario_id=session["scenario_id"]),
        media_type="application/x-ndjson",
    )


if STATIC.is_dir():
    app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")
