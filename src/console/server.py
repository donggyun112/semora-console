"""FastAPI server for the control-plane console.

Endpoints:
  GET  /api/scenarios   the fixed scenarios (with locked prompts)
  GET  /api/units       composable units, grouped-ready metadata (point, composer, verdict)
  POST /api/run         run a scenario with a chosen set of units (ndjson stream)
  POST /api/resume      approve/deny a suspended (approval) call (ndjson stream)
  POST /api/abort       operator stop: trip aborted() and cancel the in-flight attempt
  POST /api/steer       enqueue one user_steer on the run's single input queue
  POST /api/recover     continue after a simulated worker crash (commit or pre-park)

Scenario prompts are locked. Mid-run steer is the operator's one queue (capped), not a new agent.
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
from langchain_core.messages import AIMessage, HumanMessage
from nexora import Agent, AgentRuntime
from nexora.contracts import ToolCall
from nexora.contracts.types import PendingInput
from nexora.orchestrator import AgentSuspended
from pydantic import BaseModel, Field

from .dormancy import dormant_reason
from .provider import DEFAULT_MODEL, openrouter_model
from .scenarios import SCENARIOS, SYSTEM_PROMPT
from .store import SimulatedWorkerCrash, crash_before_approval, make_store
from .tools import DemoTools
from .units import LOG_HINT, UNITS, UNITS_BY_NAME, compose_controls

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


def _new_agent() -> Agent:
    """Create the model-visible definition shared by a run and its continuations."""
    return Agent(
        name="control-plane-console",
        description="Runs locked operator control-plane scenarios.",
        model=openrouter_model(),
        tools=DemoTools(),
        system_prompt=SYSTEM_PROMPT,
    )


@app.middleware("http")
async def no_store(request, call_next):
    """Never cache the console — this port is reused across demos, and a stale cached
    index.html/app.js from a different app would render a broken page."""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


class RunRequest(BaseModel):
    """A scenario id plus the control-plane units to compose."""

    scenario_id: str
    units: list[str] = []


class ResumeRequest(BaseModel):
    """An operator decision for a suspended call."""

    run_id: str
    pending_id: str
    approved: bool


class AbortRequest(BaseModel):
    """Operator stop for the current in-flight attempt."""

    run_id: str


class SteerRequest(BaseModel):
    """One operator nudge for the in-flight run's single steering queue."""

    run_id: str
    text: str = Field(min_length=1, max_length=200)


class RecoverRequest(BaseModel):
    """Continue a run after the worker died mid-round."""

    run_id: str


def _crash_point(scenario_id: str, selected: list[str]) -> str | None:
    """Where a crash-enabled scenario kills the worker.

    approval on → first ``pre_tool_use`` (tool_call already emitted, park not written).
    otherwise → first ``finish_effect`` (the effect is already committed).
    """
    if scenario_id not in {"crash", "parallel_crash"}:
        return None
    return "gate" if "approval" in selected else "commit"


def _controls(selected: list[str], run_id: str, crash_at: str | None) -> Any:
    extra = [crash_before_approval(run_id, _store)] if crash_at == "gate" else None
    return compose_controls(selected, extra_pre=extra)


def _is_aborted(run_id: str) -> bool:
    """Loop kill switch: True once the operator has posted /api/abort for this run."""
    session = _sessions.get(run_id)
    return bool(session and session.get("aborted"))


def _injected_text(payload: dict[str, Any]) -> str:
    """Pull the human-readable body out of a context_injected payload."""
    message = payload.get("message") or {}
    data = message.get("data") if isinstance(message, dict) else {}
    if not isinstance(data, dict):
        data = message if isinstance(message, dict) else {}
    content = data.get("content") if isinstance(data, dict) else None
    return content if isinstance(content, str) else str(content or "")





async def _capped(turn: int, _text: str, _calls: list[dict[str, Any]]) -> bool:
    """Bounded-turn backstop: runaway/cost guard for a public link, and a terminator for
    units like log_gate that veto completion."""
    return turn >= 8


def _frame(kind: str, **payload: Any) -> str:
    """Encode one newline-delimited stream frame."""
    return json.dumps({"kind": kind, **payload}, ensure_ascii=False, default=str) + "\n"


def _project_event(
    event: dict[str, Any],
    pending: dict[str, dict[str, Any]],
    projected_denials: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Pass tool requests through immediately. A later deny upgrades the same call to blocked.

    Holding the request until the result made the UI look frozen after ``inject user_prompt``:
    the model was already emitting a tool_call, but the console swallowed it until execution
    finished. nexora-ui shows REQUESTED at once; this matches that.
    """
    kind = event.get("type")
    if kind == "tool_call":
        pending[str(event.get("id") or "")] = event
        shown = dict(event)
        shown["pending"] = True
        return [{"kind": "agent", "event": shown}]
    if kind == "tool_result":
        call_id = str(event.get("id") or "")
        call = pending.pop(call_id, None)
        res = event.get("result") or {}
        if event.get("executed") is False:
            unit = res.get("unit", "control") if isinstance(res, dict) else "control"
            is_suspend = isinstance(res, dict) and res.get("type") == "suspend"
            unit_frame = {
                "kind": "unit",
                "unit": unit,
                "verdict": "suspend" if is_suspend else "deny",
                "message": (res.get("message") or res.get("reason") or "denied") if isinstance(res, dict) else "denied",
            }
            if is_suspend:
                return [unit_frame]
            blocked = dict(call or {"name": event.get("name"), "input": {}})
            blocked["type"] = "tool_call"
            blocked["blocked"] = True
            if projected_denials is not None and call_id in projected_denials:
                projected_denials.discard(call_id)
                return [{"kind": "agent", "event": blocked}]
            return [unit_frame, {"kind": "agent", "event": blocked}]
        frames: list[dict[str, Any]] = []
        if isinstance(res, dict) and res.get("redacted_by"):
            unit = res["redacted_by"]
            frames.append(
                {
                    "kind": "unit",
                    "unit": unit,
                    "verdict": "block" if unit == "context_firewall" else "rewrite",
                    "message": res.get("control_note") or "도구 결과를 다시 썼습니다",
                }
            )
        frames.append({"kind": "agent", "event": event})
        return frames
    if kind in {"text", "thinking"}:
        return [{"kind": "agent", "event": event}]
    return [{"kind": "agent", "event": event}]


async def _stream(
    run_id: str, attempt: Any, *, selected: list[str], scenario_id: str, open_session: bool = False,
) -> AsyncIterator[str]:
    """Run one attempt, translate events into frames, and end with a policy summary."""
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    fired: dict[str, int] = {}
    pending_calls: dict[str, dict[str, Any]] = {}
    projected_denials: set[str] = set()

    def mark(unit: str) -> None:
        fired[unit] = fired.get(unit, 0) + 1

    async def on_event(event: dict[str, Any]) -> None:
        for frame in _project_event(event, pending_calls, projected_denials):
            if frame.get("kind") == "unit":
                mark(str(frame.get("unit") or "control"))
            await queue.put(frame)

    async def publish(event_type: str, payload: dict[str, Any]) -> None:
        # One queue: operator user_steer and policy Proceed both admit as context_injected.
        if str(event_type).endswith("context_injected"):
            kind = str(payload.get("kind") or "")
            if kind in {"control", "user_steer"}:
                text = _injected_text(payload)
                if kind == "control" and LOG_HINT in text and "log_gate" in selected:
                    mark("log_gate")
                    await queue.put(
                        {"kind": "unit", "unit": "log_gate", "verdict": "steer",
                         "message": "종료 거부"}
                    )
                await queue.put(
                    {"kind": "steer", "source": kind, "text": text, "phase": "admitted"}
                )
        if str(event_type).endswith("permission_denied"):
            reason = payload.get("reason") or {}
            call_id = str(payload.get("call_id") or "")
            if call_id and call_id not in projected_denials:
                unit = reason.get("unit", "control") if isinstance(reason, dict) else "control"
                message = (
                    reason.get("message") or reason.get("reason") or "denied"
                    if isinstance(reason, dict)
                    else "denied"
                )
                projected_denials.add(call_id)
                mark(str(unit))
                await queue.put(
                    {"kind": "unit", "unit": unit, "verdict": "deny", "message": message}
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
        if open_session:
            await runtime.events.session_start("console")
        try:
            outcome = await attempt(runtime, on_event)
            reason = outcome.get("stop_reason") if isinstance(outcome, dict) else "completed"
            await runtime.events.session_end(str(reason or "completed"))
            await queue.put({"kind": "outcome", "outcome": outcome})
            if selected:
                await queue.put(summary_frame())
        except AgentSuspended as stopped:
            await queue.put({"kind": "suspended", "pending_id": stopped.pending_id, "tool_call_id": stopped.tool_call_id})
        except SimulatedWorkerCrash as crashed:
            await queue.put(
                {
                    "kind": "recoverable",
                    "step": crashed.step,
                    "message": "워커 장애",
                }
            )
        except asyncio.CancelledError:
            await runtime.events.session_end("aborted" if _is_aborted(run_id) else "cancelled")
            if _is_aborted(run_id):
                await queue.put({"kind": "outcome", "outcome": {"stop_reason": "aborted"}})
            raise
        except Exception as failure:  # noqa: BLE001 - surface any failure as a frame
            await runtime.events.session_end("error")
            await queue.put({"kind": "error", "message": str(failure)})
        finally:
            await queue.put(None)

    task = asyncio.create_task(run_attempt())
    session = _sessions.get(run_id)
    if session is not None:
        session["task"] = task
    try:
        while (item := await queue.get()) is not None:
            yield _frame(**item)
    finally:
        if session is not None:
            session.pop("task", None)
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
    crash_at = _crash_point(request.scenario_id, selected)
    prompt_id = f"{run_id}:prompt:{uuid.uuid4().hex[:8]}"
    agent = _new_agent()
    _sessions[run_id] = {
        "units": selected, "agent": agent, "scenario_id": request.scenario_id,
        "aborted": False, "history": None, "crash": crash_at is not None, "crash_at": crash_at,
    }
    if crash_at is not None:
        _store.arm(run_id, at=crash_at)

    async def attempt(runtime: AgentRuntime, on_event: Any) -> dict[str, Any]:
        text: list[str] = []
        calls: list[ToolCall] = []

        async def capture(event: dict[str, Any]) -> None:
            await on_event(event)
            if crash_at is None:
                return
            if event.get("type") == "text":
                text.append(str(event.get("text") or ""))
            if event.get("type") == "tool_call":
                calls.append(
                    {
                        "id": str(event.get("id") or ""),
                        "name": str(event.get("name") or ""),
                        "args": event.get("input") or {},
                        "type": "tool_call",
                    }
                )
                _sessions[run_id]["history"] = [
                    HumanMessage(scenario["prompt"], id=prompt_id),
                    AIMessage(content="".join(text), tool_calls=list(calls)),
                ]

        return await runtime.run(
            run_id, agent, scenario["prompt"],
            controls=_controls(selected, run_id, crash_at), on_event=capture,
            should_stop_after_turn=_capped, aborted=lambda: _is_aborted(run_id),
            prompt_id=prompt_id,
        )

    return StreamingResponse(
        _stream(run_id, attempt, selected=selected, scenario_id=request.scenario_id, open_session=True),
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

    session["aborted"] = False
    if session.get("crash") and session.get("crash_at") != "gate":
        _store.arm(request.run_id, at="commit")

    async def attempt(runtime: AgentRuntime, on_event: Any) -> dict[str, Any]:
        agent = session["agent"]
        return await runtime.resume(
            request.run_id, request.pending_id, answer, agent.model, agent.tools,
            controls=compose_controls(session["units"]), on_event=on_event,
            system_prompt=agent.system_prompt, should_stop_after_turn=_capped,
            aborted=lambda: _is_aborted(request.run_id),
        )

    return StreamingResponse(
        _stream(request.run_id, attempt, selected=session["units"], scenario_id=session["scenario_id"]),
        media_type="application/x-ndjson",
    )


@app.post("/api/recover")
async def recover(request: RecoverRequest) -> StreamingResponse:
    """Finish a run whose worker died after a tool_call, before or after the effect."""
    session = _sessions.get(request.run_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown run_id")
    history = session.get("history")
    if not history:
        raise HTTPException(status_code=409, detail="복원할 장애가 없습니다")

    async def attempt(runtime: AgentRuntime, on_event: Any) -> dict[str, Any]:
        agent = session["agent"]
        outcome = await runtime.recover(
            request.run_id, history, agent.model, agent.tools,
            controls=compose_controls(session["units"]), on_event=on_event,
            retry_running=False, system_prompt=agent.system_prompt,
            should_stop_after_turn=_capped, aborted=lambda: _is_aborted(request.run_id),
        )
        session["history"] = None
        return outcome

    return StreamingResponse(
        _stream(request.run_id, attempt, selected=session["units"], scenario_id=session["scenario_id"]),
        media_type="application/x-ndjson",
    )


@app.post("/api/abort")
async def abort(request: AbortRequest) -> dict[str, bool]:
    """Trip aborted() and cancel the live attempt. Works for any scenario, any units."""
    session = _sessions.get(request.run_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown run_id")
    session["aborted"] = True
    task = session.get("task")
    if isinstance(task, asyncio.Task) and not task.done():
        task.cancel()
    return {"ok": True}


@app.post("/api/steer")
async def steer(request: SteerRequest) -> dict[str, bool]:
    """Enqueue a user_steer on the in-flight run. One queue; next drain admits it."""
    session = _sessions.get(request.run_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown run_id")
    task = session.get("task")
    if not isinstance(task, asyncio.Task) or task.done():
        raise HTTPException(status_code=409, detail="no in-flight run")
    text = request.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty steer")
    runtime = AgentRuntime(store=_store)
    await runtime.submit(request.run_id, PendingInput("user_steer", HumanMessage(text)))
    return {"ok": True}


if STATIC.is_dir():
    app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")
