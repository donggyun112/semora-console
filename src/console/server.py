"""FastAPI server for the control-plane console.

Endpoints:
  GET  /api/scenarios   the fixed scenarios (with locked prompts)
  GET  /api/units       composable units, grouped-ready metadata (point, composer, verdict)
  POST /api/run         run a scenario with a chosen set of units (ndjson stream)
  POST /api/resume      approve/deny a suspended (approval) call (ndjson stream)
  POST /api/abort       operator stop: trip aborted() and cancel the in-flight attempt
  POST /api/steer       enqueue one user_steer on the run's single input queue
  POST /api/recover     continue after a simulated worker crash (commit or pre-park)
  POST /api/fork        create a new execution version from one event checkpoint

Scenario prompts are locked. Mid-run steer is the operator's one queue (capped), not a new agent.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import os
import re
import socket
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from dotenv import dotenv_values, load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import HumanMessage
from semora import Agent, AgentRuntime, new_run_id
from semora.contracts.types import PendingInput
from semora.dispatch import Answer, Prompt, Recover
from semora.orchestrator import AgentSuspended, Orchestrator
from semora_store import Contended, Fenced, Indeterminate
from semora.transcript import SCHEMA_VERSION, entry_id
from semora_fork import RERUNS, read_event_checkpoint
from pydantic import BaseModel, Field

from .dormancy import dormant_reason
from .fork_demo import (
    EventCheckpointProjector,
    branch_snapshot,
    run_from_event,
)
from .provider import model_name, openrouter_model
from .scenarios import SCENARIOS, SYSTEM_PROMPT
from .store import SimulatedWorkerCrash, crash_before_approval, make_store
from .tools import DemoTools
from .units import LOG_HINT, UNITS, UNITS_BY_NAME, compose_controls

load_dotenv()
# MODEL is edited in .env while trying models out, so let that edit beat a stale
# exported value. Credentials and DATABASE_URL keep normal precedence — overriding
# those let a leftover .env silently replace the key or the durable-proof DSN.
if (_dotenv_model := dotenv_values().get("MODEL")):
    os.environ["MODEL"] = _dotenv_model

# Two workers sharing a ledger must not share an owner name, or each renews the other's
# lease and the contention this demo is meant to show never happens.
WORKER = os.getenv("CONSOLE_WORKER") or socket.gethostname()

# How long a worker's claim on a run outlives the worker. Sixty seconds is right for a
# deployment and useless for a demo, where nobody watches a paused container for a
# minute, so the seam is a knob rather than a constant.
LEASE_TTL = float(os.getenv("CONSOLE_LEASE_TTL") or 60.0)

STATIC = Path(__file__).parent / "static"
_SCENARIO_BY_ID = {s["id"]: s for s in SCENARIOS}

_sessions: dict[str, dict[str, Any]] = {}
_store: Any = None
_transcript: Any = None
_closer: Any = None


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Open the runtime stores on startup and close them on shutdown."""
    global _store, _transcript, _closer
    _store, _transcript, _closer = await make_store()
    yield
    if _closer is not None:
        await _closer()


app = FastAPI(title="Semora Control Plane Console", lifespan=lifespan)


def _new_agent(session_id: str) -> Agent:
    """Create the run agent. Payment records are keyed by scenario id so a rerun can reuse them."""
    return Agent(
        name="control-plane-console",
        description="Runs locked operator control-plane scenarios.",
        model=openrouter_model(),
        tools=DemoTools(session=_session_step(session_id)),
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
    operator: str = ""
    """Who is driving, so their payment records are their own. Anything outside
    [a-z0-9_-] is dropped; empty means the ledger is shared, as it was."""


class ResumeRequest(BaseModel):
    """An operator decision for a suspended call."""

    run_id: str
    pending_id: str
    approved: bool
    units: list[str] | None = None
    """The policy in force at the moment of the decision. A suspension's window is open
    ended, so the operator may have changed it since the call parked."""


class AbortRequest(BaseModel):
    """Operator stop for the current in-flight attempt."""

    run_id: str


class SteerRequest(BaseModel):
    """One operator nudge for the in-flight run's single steering queue."""

    run_id: str
    text: str = Field(min_length=1, max_length=200)


_OPERATOR = re.compile(r"[^a-z0-9_-]")


def _session_id(operator: str, scenario_id: str) -> str:
    """The run whose steps this operator's effects are.

    One session per person per scenario, so a charge outlives the agent run that made it
    without outliving the visitor who made it. On a link two people can open at once,
    sharing this would mean the first one charges and everyone after replays it.
    """
    who = _OPERATOR.sub("", operator.strip().lower())[:24]
    return f"session:{who}:{scenario_id}" if who else f"session:{scenario_id}"


def _session_step(session_id: str) -> Any:
    """``Orchestrator.run`` for that session, taken and released per effect.

    A fresh attempt each time rather than one held open: nothing else holds this run's
    lease, and a worker that grabbed it for the length of an agent turn would contend
    with its own tools.
    """

    async def run(step: str, fn: Any) -> Any:
        orchestrator = Orchestrator(session_id, _store, owner=WORKER, ttl=LEASE_TTL)
        return await orchestrator.run(step, fn)

    return run


class RecoverRequest(BaseModel):
    """Continue a run after the worker died mid-round."""

    run_id: str


class ForkRequest(BaseModel):
    """Create one execution version from a completed run's observation edge."""

    run_id: str
    event_id: str
    edge: Literal["before", "after"] = "before"
    units: list[str]
    # Skip the gate and re-run only the journal over the recorded result. Sent when the
    # operator changed nothing that lives at the gate.
    rejournal: bool = False


_SESSION_KEY = "console:session"

# What a later process needs to continue a run. `agent` is rebuilt from `scenario_id`,
# and the projector's event bookkeeping is per-process, so neither is stored.
_DURABLE_SESSION = (
    "units", "scenario_id", "session_id", "conversation_id", "origin_id", "source_run_id",
    "origin_runs", "default_fork_origin", "crash", "crash_at", "terminal",
    "fork_parent", "fork_event_id", "fork_edge", "fork_mode",
    # Which coordinates a fork may start from. Rebuilt only by watching a run stream, so
    # a worker that never saw the run had an empty map and refused every branch — the
    # transcript still held the events it was refusing to reach.
    "forkable_events",
)


async def _remember_session(run_id: str, session: dict[str, Any]) -> None:
    """Write the run's console state to the ledger beside the runtime's own.

    ``_sessions`` is a cache of this, not the record. Without ``DATABASE_URL`` the ledger
    is memory too and a restart loses the run exactly as the runtime does; with it, a
    process that never saw the run can still resume it.
    """
    await _store.write_control(
        run_id, _SESSION_KEY, {k: session[k] for k in _DURABLE_SESSION if k in session}
    )


async def _session(run_id: str) -> dict[str, Any]:
    """The live session, rehydrated from the ledger when this process never saw the run."""
    session = _sessions.get(run_id)
    if session is not None:
        return session
    record = await _store.read(run_id, _SESSION_KEY)
    if record.status != "done" or not isinstance(record.value, dict):
        raise HTTPException(status_code=404, detail="unknown run_id")
    stored = dict(record.value)
    forkable = stored.get("forkable_events") or {}
    session = {
        **stored,
        # event_ids is the same set of keys; it is a membership check, not a second fact.
        "event_ids": set(forkable),
        "forkable_events": dict(forkable),
        # The session, not the scenario: a resumed run must rejoin the session whose
        # steps its effects are, or the charge it already made is invisible to it.
        "agent": _new_agent(
            str(stored.get("session_id") or f"session:{stored.get('scenario_id')}")
        ),
        "aborted": False,
    }
    _sessions[run_id] = session
    return session


def _crash_point(scenario_id: str, selected: list[str]) -> str | None:
    """Where a crash-enabled scenario kills the worker.

    approval on → first ``pre_tool_use`` (tool_call already emitted, park not written).
    unknown_effect → between ``start`` and ``finish_effect``, the only seam that leaves a
    step running, so recovery has something it genuinely cannot decide.
    otherwise → first ``finish_effect`` (the effect is already committed).
    """
    if scenario_id == "unknown_effect":
        return "effect"
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


async def _publish_source_branch(
    runtime: AgentRuntime,
    *,
    run_id: str,
    conversation_id: str,
    origin_id: str,
) -> None:
    """Expose the completed source lineage so the UI can compare it with a fork."""
    history = await runtime.committed_history(run_id, conversation_id)
    snapshot = branch_snapshot("source", run_id, conversation_id, origin_id, history)
    await runtime.events.publish("branch_snapshot", **snapshot)


def _project_event(
    event: dict[str, Any],
    pending: dict[str, dict[str, Any]],
    projected_denials: set[str] | None = None,
    announced_rewrites: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Pass tool requests through immediately. A later deny upgrades the same call to blocked.

    Holding the request until the result made the UI look frozen after ``inject user_prompt``:
    the model was already emitting a tool_call, but the console swallowed it until execution
    finished. semora-ui shows REQUESTED at once; this matches that.
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
                "message": _verdict_message(res),
                # Which call this verdict gates. Without it a parallel batch shows N
                # identical SUSPEND rows and the operator cannot tell them apart.
                "call_id": call_id,
                "name": event.get("name") or (call or {}).get("name"),
                "input": (call or {}).get("input") or {},
            }
            if is_suspend:
                return [unit_frame]
            blocked = dict(call or {"name": event.get("name"), "input": {}})
            blocked["id"] = call_id
            blocked["type"] = "tool_call"
            blocked["blocked"] = True
            blocked_frame = {
                "kind": "agent",
                "event": blocked,
                "call_id": call_id,
                "checkpoint_phase": "tool_result",
            }
            # The refusal is what went back to the model, so it is that call's result and
            # the trace says so. Without it a denied boundary showed a call and a gate and
            # then nothing, while every other call ended in a line naming its answer.
            refused_frame = {"kind": "agent", "event": event}
            if projected_denials is not None and call_id in projected_denials:
                projected_denials.discard(call_id)
                return [blocked_frame, refused_frame]
            return [unit_frame, blocked_frame, refused_frame]
        frames: list[dict[str, Any]] = []
        rewritten = isinstance(res, dict) and res.get("redacted_by")
        if rewritten and announced_rewrites is not None and call_id in announced_rewrites:
            announced_rewrites.discard(call_id)
            rewritten = False
        if rewritten:
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


def _verdict_message(res: Any) -> str:
    """The gate's own words, marked when the refusal came after an approval."""
    if not isinstance(res, dict):
        return "denied"
    message = res.get("message") or res.get("reason") or "denied"
    return f"승인 후 재검증 — {message}" if res.get("revalidated") else str(message)


def _register_frame(session: dict[str, Any], frame: dict[str, Any]) -> None:
    """Index a visible event and every earlier event whose restore point it finalizes."""
    event_id = frame.get("event_id")
    if event_id:
        session.setdefault("event_ids", set()).add(str(event_id))
        if frame.get("forkable"):
            session.setdefault("forkable_events", {})[str(event_id)] = frame[
                "restore_edge"
            ]
    for update in frame.get("restore_updates", []):
        restored_id = str(update["event_id"])
        session.setdefault("event_ids", set()).add(restored_id)
        session.setdefault("forkable_events", {})[restored_id] = update["restore_edge"]


CONSOLE_FRAME = "console_frame"
"""A frame kept beside the conversation it describes.

The transcript already carries entries that are not messages — a fork checkpoint is one —
and its readers skip a kind they do not know, by contract. Frames go here rather than into
a table of their own because a fork continues its parent's conversation: one read returns
every version's trace in the order it happened, which is exactly what the console had been
holding in browser memory and losing on reload.
"""


async def _keep_frame(conversation_id: str, run_id: str, sequence: int, frame: dict[str, Any]) -> None:
    """Persist one rendered frame. Unchained, so it never joins the model's branch."""
    body = {"type": CONSOLE_FRAME, "sequence": sequence, "frame": frame}
    await _transcript.append(
        {
            "uuid": entry_id(f"{run_id}:{sequence}", body),
            "conversation_id": conversation_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "schema_version": SCHEMA_VERSION,
            "metadata": {"run_id": run_id},
            **body,
        }
    )


def _kept_frames(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every frame this conversation kept, in the order they were rendered."""
    return [
        entry["frame"]
        for entry in entries
        if entry.get("type") == CONSOLE_FRAME and isinstance(entry.get("frame"), dict)
    ]


async def _stream(
    run_id: str, attempt: Any, *, selected: list[str], scenario_id: str, open_session: bool = False,
) -> AsyncIterator[str]:
    """Run one attempt, translate events into frames, and end with a policy summary."""
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    fired: dict[str, int] = {}
    pending_calls: dict[str, dict[str, Any]] = {}
    projected_denials: set[str] = set()
    announced_rewrites: set[str] = set()
    replayed_calls: set[str] = set()
    session = _sessions.get(run_id)
    projector = None
    if session is not None and {
        "conversation_id", "origin_runs", "default_fork_origin"
    } <= session.keys():
        projector = session.get("event_projector")
        if projector is None:
            projector = EventCheckpointProjector(
                _transcript,
                run_id=run_id,
                conversation_id=session["conversation_id"],
                origin_runs=session["origin_runs"],
                default_origin_id=session["default_fork_origin"],
            )
            session["event_projector"] = projector

    kept = 0

    async def put(frame: dict[str, Any]) -> None:
        nonlocal kept
        frame = {**frame, "run_id": run_id}
        if projector is not None:
            frame = await projector.stamp(frame)
        if session is not None:
            _register_frame(session, frame)
            conversation_id = session.get("conversation_id")
            if conversation_id:
                await _keep_frame(str(conversation_id), run_id, kept, frame)
                kept += 1
        await queue.put(frame)

    def mark(unit: str) -> None:
        fired[unit] = fired.get(unit, 0) + 1
    async def on_event(event: dict[str, Any]) -> None:
        for frame in _project_event(
            event, pending_calls, projected_denials, announced_rewrites
        ):
            if frame.get("kind") == "unit":
                mark(str(frame.get("unit") or "control"))
            await put(frame)

    async def publish(event_type: str, payload: dict[str, Any]) -> None:
        # One queue: operator user_steer and policy Proceed both admit as context_injected.
        if str(event_type).endswith("context_injected"):
            kind = str(payload.get("kind") or "")
            if kind in {"control", "user_steer"}:
                text = _injected_text(payload)
                if kind == "control" and LOG_HINT in text and "log_gate" in selected:
                    mark("log_gate")
                    await put(
                        {"kind": "unit", "unit": "log_gate", "verdict": "steer",
                         "message": "종료 거부"}
                    )
                await put(
                    {"kind": "steer", "source": kind, "text": text, "phase": "admitted"}
                )
        async def announce(call_id: str) -> None:
            # A replayed call is executed from the record, so the loop emits its hooks and
            # no agent events at all — the trace showed a gate and a boundary with nothing
            # between them, and the chat had no card for a tool that ran. The hook payload
            # names the call, so the console says what happened rather than leaving the
            # operator to infer it from two lifecycle rows. Announced at the first hook
            # that names it: the gate, or — when a branch skipped the gate to journal the
            # result again — the boundary itself.
            if not call_id or call_id in pending_calls or call_id in replayed_calls:
                return
            replayed_calls.add(call_id)
            call = {
                "type": "tool_call",
                "id": call_id,
                "name": payload.get("name"),
                "input": payload.get("input") or {},
            }
            pending_calls[call_id] = call
            await put({"kind": "agent", "event": {**call, "pending": True}})

        if str(event_type).endswith(("pre_tool_use", "post_tool_use", "post_tool_use_failure")):
            await announce(str(payload.get("call_id") or ""))

        rewrite: dict[str, Any] | None = None
        if str(event_type).endswith(("post_tool_use", "post_tool_use_failure")):
            # A replayed call never re-emits an agent tool_result, so the rewrite that
            # a journal unit performed here would go unrecorded — the trace showed a
            # masked answer and no policy that did the masking. Read off the boundary
            # instead, and skipped later for calls that do report one.
            result = payload.get("result")
            unit = result.get("redacted_by") if isinstance(result, dict) else None
            call_id = str(payload.get("call_id") or "")
            if unit:
                announced_rewrites.add(call_id)
                rewrite = {
                    "kind": "unit",
                    "unit": str(unit),
                    "verdict": "block" if unit == "context_firewall" else "rewrite",
                    "message": result.get("control_note") or "도구 결과를 다시 썼습니다",
                    "call_id": call_id,
                    "name": payload.get("name"),
                }
        if str(event_type).endswith("permission_denied"):
            reason = payload.get("reason") or {}
            call_id = str(payload.get("call_id") or "")
            if call_id and call_id not in projected_denials:
                unit = reason.get("unit", "control") if isinstance(reason, dict) else "control"
                projected_denials.add(call_id)
                mark(str(unit))
                await put(
                    {
                        "kind": "unit",
                        "unit": unit,
                        "verdict": "deny",
                        "message": _verdict_message(reason),
                        "call_id": call_id,
                        "name": payload.get("name"),
                    }
                )
        await put({"kind": "lifecycle", "type": str(event_type), "payload": payload})
        replayed_result = None
        if str(event_type).endswith(("post_tool_use", "post_tool_use_failure")):
            call_id = str(payload.get("call_id") or "")
            if call_id in replayed_calls:
                replayed_calls.discard(call_id)
                pending_calls.pop(call_id, None)
                result = payload.get("result")
                if payload.get("replayed") and isinstance(result, dict):
                    # The runtime restored this result from a record. The badge reads
                    # the tool's own replay mark, so the record's mark is set beside it.
                    result = {
                        **result,
                        "execution": {**(result.get("execution") or {}), "replayed": True},
                    }
                replayed_result = {
                    "kind": "agent",
                    "event": {
                        "type": "tool_result",
                        "id": call_id,
                        "name": payload.get("name"),
                        "executed": not str(event_type).endswith("post_tool_use_failure"),
                        "result": result,
                    },
                }
        if rewrite is not None:
            # After the hook, because the unit runs inside it: pii_mask is an
            # post_tool_use, so a row above post_tool_use would date it wrong.
            mark(str(rewrite["unit"]))
            await put(rewrite)
        if replayed_result is not None:
            await put(replayed_result)

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
        runtime = AgentRuntime(
            store=_store,
            transcript=_transcript,
            emit=publish,
            owner=WORKER,
            lease_ttl=LEASE_TTL,
        )
        meta = {"kind": "meta", "run_id": run_id, "units": selected}
        if session is not None and session.get("fork_parent"):
            meta.update(
                {
                    "fork_parent": session["fork_parent"],
                    "fork_event_id": session["fork_event_id"],
                    "fork_edge": session["fork_edge"],
                    "fork_mode": session["fork_mode"],
                }
            )
        await put(meta)
        if open_session:
            await runtime.events.session_start("console")
        try:
            outcome = await attempt(runtime, on_event)
            reason = outcome.get("stop_reason") if isinstance(outcome, dict) else "completed"
            await runtime.events.session_end(str(reason or "completed"))
            completed = _sessions.get(run_id)
            if completed is not None:
                completed["terminal"] = True
                await _remember_session(run_id, completed)
            await put({"kind": "outcome", "outcome": outcome})
            if selected:
                await put(summary_frame())
        except AgentSuspended as stopped:
            await put({"kind": "suspended", "pending_id": stopped.pending_id, "tool_call_id": stopped.tool_call_id})
        except Fenced as stale:
            await put(
                {
                    "kind": "fenced",
                    "worker": WORKER,
                    "message": "이 워커의 차례는 지났습니다 — 실행은 다른 워커에게 넘어갔습니다",
                    "presented": getattr(stale, "presented", None),
                    "issued": getattr(stale, "issued", None),
                }
            )
        except Contended:
            await put(
                {
                    "kind": "contended",
                    "worker": WORKER,
                    "message": "다른 워커가 이 실행을 잡고 있습니다",
                }
            )
        except Indeterminate as unknown:
            await put(
                {
                    "kind": "indeterminate",
                    "step": unknown.step,
                    "message": "이 효과는 나갔을 수도, 안 나갔을 수도 있습니다",
                }
            )
        except SimulatedWorkerCrash as crashed:
            await put(
                {
                    "kind": "recoverable",
                    "step": crashed.step,
                    "message": "워커 장애",
                }
            )
        except asyncio.CancelledError:
            await runtime.events.session_end("aborted" if _is_aborted(run_id) else "cancelled")
            if _is_aborted(run_id):
                await put({"kind": "outcome", "outcome": {"stop_reason": "aborted"}})
            raise
        except Exception as failure:  # noqa: BLE001 - surface any failure as a frame
            await runtime.events.session_end("error")
            await put({"kind": "error", "message": str(failure)})
        finally:
            await queue.put(None)

    task = asyncio.create_task(run_attempt())
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
        "model": model_name(),
        "units": [
            {"name": u.name, "point": u.point, "composer": u.composer,
             "verdict": u.verdict, "title": u.title, "desc": u.desc}
            for u in UNITS
        ],
        # From each point a branch can resume at, the control points that run again over
        # the recorded round — the framework's table, so the page never guesses which of
        # the selected policies a branch will reach.
        "reruns": {point: list(points) for point, points in RERUNS.items()},
    }


@app.post("/api/run")
async def run(request: RunRequest) -> StreamingResponse:
    """Start a scenario run with the chosen units."""
    scenario = _SCENARIO_BY_ID.get(request.scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="unknown scenario_id")
    # Time-ordered, so a ledger row sorts by when the run started rather than by chance.
    run_id = new_run_id()
    selected = [u for u in request.units if u in UNITS_BY_NAME]
    crash_at = _crash_point(request.scenario_id, selected)
    prompt_id = f"{run_id}:prompt:{uuid.uuid4().hex[:8]}"
    session_id = _session_id(request.operator, request.scenario_id)
    agent = _new_agent(session_id)
    conversation_id = f"conv-{uuid.uuid4().hex[:12]}"
    origin_runs = {prompt_id: run_id}
    _sessions[run_id] = {
        "units": selected, "agent": agent, "scenario_id": request.scenario_id,
        "session_id": session_id,
        "aborted": False, "crash": crash_at is not None, "crash_at": crash_at,
        "conversation_id": conversation_id, "origin_id": prompt_id,
        "source_run_id": run_id,
        "origin_runs": origin_runs,
        "default_fork_origin": prompt_id,
        "event_ids": set(),
        "forkable_events": {},
        "terminal": False,
    }
    await _remember_session(run_id, _sessions[run_id])
    if crash_at is not None:
        # The effect seam watches the session, because that is where a charge is a step.
        # The others watch the agent run they interrupt.
        _store.arm(session_id if crash_at == "effect" else run_id, at=crash_at)

    async def attempt(runtime: AgentRuntime, on_event: Any) -> dict[str, Any]:
        outcome = await runtime.dispatch(
            run_id,
            agent,
            Prompt(scenario["prompt"], prompt_id=prompt_id),
            conversation_id=conversation_id,
            controls=_controls(selected, run_id, crash_at),
            on_event=on_event,
            should_stop_after_turn=_capped,
            aborted=lambda: _is_aborted(run_id),
        )
        await _publish_source_branch(
            runtime,
            run_id=run_id,
            conversation_id=conversation_id,
            origin_id=prompt_id,
        )
        return outcome

    return StreamingResponse(
        _stream(run_id, attempt, selected=selected, scenario_id=request.scenario_id, open_session=True),
        media_type="application/x-ndjson",
    )


@app.post("/api/fork")
async def fork(request: ForkRequest) -> StreamingResponse:
    """Re-run a completed scenario from the selected durable event coordinate."""
    source = await _session(request.run_id)
    if not source.get("terminal"):
        raise HTTPException(status_code=409, detail="run is not forkable")
    if request.event_id not in source.get("event_ids", set()):
        raise HTTPException(status_code=404, detail="event does not belong to run version")
    restore_edge = source.get("forkable_events", {}).get(request.event_id)
    if restore_edge is None:
        raise HTTPException(status_code=409, detail="event is not an exact restore point")
    if request.edge != restore_edge:
        raise HTTPException(
            status_code=409,
            detail=f"event is only restorable from its {restore_edge} edge",
        )
    try:
        checkpoint = await read_event_checkpoint(
            _transcript, source["conversation_id"], request.event_id
        )
    except ValueError as failure:
        raise HTTPException(status_code=404, detail=str(failure)) from failure
    coordinate = checkpoint.before if request.edge == "before" else checkpoint.after

    fork_run_id = new_run_id()
    fork_units = [name for name in request.units if name in UNITS_BY_NAME]
    fork_mode = "input" if coordinate.origin_id is not None else "leaf"
    child_origin_runs = dict(source["origin_runs"])
    if coordinate.origin_id is not None:
        child_origin_runs[coordinate.origin_id] = fork_run_id
    source["forked_to"] = fork_run_id
    source.setdefault("fork_children", []).append(fork_run_id)
    _sessions[fork_run_id] = {
        "units": fork_units,
        "agent": source["agent"],
        "scenario_id": source["scenario_id"],
        "aborted": False,
        "crash": False,
        "crash_at": None,
        "conversation_id": source["conversation_id"],
        "origin_id": coordinate.origin_id,
        "source_run_id": request.run_id,
        "origin_runs": child_origin_runs,
        "default_fork_origin": coordinate.origin_id,
        "fork_parent": request.run_id,
        "fork_event_id": request.event_id,
        "fork_edge": request.edge,
        "fork_mode": fork_mode,
        "fork_rejournal": request.rejournal,
        "event_ids": set(),
        "forkable_events": {},
        "terminal": False,
    }

    await _remember_session(fork_run_id, _sessions[fork_run_id])

    async def attempt(runtime: AgentRuntime, on_event: Any) -> dict[str, Any]:
        return await run_from_event(
            runtime,
            _store,
            _transcript,
            event_id=request.event_id,
            edge=request.edge,
            run_id=fork_run_id,
            conversation_id=source["conversation_id"],
            agent=source["agent"],
            controls=compose_controls(fork_units),
            on_event=on_event,
            should_stop_after_turn=_capped,
            aborted=lambda: _is_aborted(fork_run_id),
            rejournal=request.rejournal,
        )

    return StreamingResponse(
        _stream(
            fork_run_id,
            attempt,
            selected=fork_units,
            scenario_id=source["scenario_id"],
            open_session=True,
        ),
        media_type="application/x-ndjson",
    )


@app.post("/api/resume")
async def resume(request: ResumeRequest) -> StreamingResponse:
    """Approve or deny a suspended call and continue the run."""
    session = await _session(request.run_id)
    answer = (
        {"type": "text", "text": "approved by the human"}
        if request.approved
        else {"type": "error", "message": "denied by the human"}
    )

    session["aborted"] = False
    if request.units is not None:
        session["units"] = [name for name in request.units if name in UNITS_BY_NAME]
        await _remember_session(request.run_id, session)
    if session.get("crash") and session.get("crash_at") != "gate":
        _store.arm(request.run_id, at="commit")

    async def attempt(runtime: AgentRuntime, on_event: Any) -> dict[str, Any]:
        return await runtime.dispatch(
            request.run_id,
            session["agent"],
            Answer(request.pending_id, answer),
            conversation_id=session["conversation_id"],
            controls=compose_controls(session["units"]),
            on_event=on_event,
            should_stop_after_turn=_capped,
            aborted=lambda: _is_aborted(request.run_id),
        )

    return StreamingResponse(
        _stream(request.run_id, attempt, selected=session["units"], scenario_id=session["scenario_id"]),
        media_type="application/x-ndjson",
    )


@app.post("/api/recover")
async def recover(request: RecoverRequest) -> StreamingResponse:
    """Finish a run whose worker died after a tool_call, before or after the effect."""
    session = await _session(request.run_id)
    session["aborted"] = False

    async def attempt(runtime: AgentRuntime, on_event: Any) -> dict[str, Any]:
        # Journal replay, not recover(history, ...). A crashed round's assistant turn is
        # deliberately absent from the transcript — recording it before execution would
        # make a failed round a transcript fact — so the history a host could hand over
        # is empty and the durable model step is what reconstructs the round.
        return await runtime.dispatch(
            request.run_id,
            session["agent"],
            Recover(),
            conversation_id=session["conversation_id"],
            controls=compose_controls(session["units"]),
            on_event=on_event,
            should_stop_after_turn=_capped,
            aborted=lambda: _is_aborted(request.run_id),
        )

    return StreamingResponse(
        _stream(request.run_id, attempt, selected=session["units"], scenario_id=session["scenario_id"]),
        media_type="application/x-ndjson",
    )


async def _drop_queued_inputs(run_id: str) -> int:
    """Make anything still waiting terminal, and report how much was dropped.

    Order matters: an input admitted while the loop is winding down is an instruction
    aimed at a run the operator has already killed, and it would be waiting there for
    whoever recovers next. Empty the queue first, then stop.
    """
    waiting = [
        record.input_id
        for record in await _store.list_inputs(run_id)
        if record.status in {"pending", "claimed"}
    ]
    if waiting:
        await _store.discard_inputs(run_id, waiting)
    return len(waiting)


@app.get("/api/runs/{run_id}/frames")
async def kept_frames(run_id: str) -> dict[str, Any]:
    """Every frame of the conversation this run belongs to, oldest first.

    A fork continues its parent's conversation, so this returns the whole version chain —
    the same sequence the browser accumulates while streaming. Reloading the page replays
    it instead of starting from nothing.
    """
    session = await _session(run_id)
    entries = await _transcript.read(str(session["conversation_id"]))
    return {
        "run_id": run_id,
        "scenario_id": session.get("scenario_id"),
        "units": session.get("units") or [],
        "frames": _kept_frames(entries),
    }


@app.post("/api/abort")
async def abort(request: AbortRequest) -> dict[str, Any]:
    """Drop what is queued, then trip aborted() and cancel the live attempt."""
    session = await _session(request.run_id)
    dropped = await _drop_queued_inputs(request.run_id)
    session["aborted"] = True
    task = session.get("task")
    if isinstance(task, asyncio.Task) and not task.done():
        task.cancel()
    return {"ok": True, "dropped": dropped}


@app.post("/api/steer")
async def steer(request: SteerRequest) -> dict[str, Any]:
    """Enqueue a user_steer. One queue; the next drain admits it.

    The queue is the ledger, not this process, so a worker that is not the one executing
    can still take the steer — which is the point once there is more than one of them.
    Whether a live attempt happens to be here only decides how soon it lands.
    """
    session = await _session(request.run_id)
    text = request.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty steer")
    if session.get("terminal"):
        raise HTTPException(status_code=409, detail="run already finished")
    runtime = AgentRuntime(
        store=_store, transcript=_transcript, owner=WORKER, lease_ttl=LEASE_TTL
    )
    # headless, not interactive. Interactive submit treats operator input as a
    # replacement: on a parked run it cancels the approval that is waiting and switches
    # the run to the new prompt. A steer is a note added to the queue, never a decision
    # taken on the operator's behalf about the call in front of them.
    await runtime.submit(
        request.run_id,
        PendingInput("user_steer", HumanMessage(text)),
        input_mode="headless",
    )
    task = session.get("task")
    live = isinstance(task, asyncio.Task) and not task.done()
    return {"ok": True, "admits": "next_drain" if live else "on_resume"}


if STATIC.is_dir():
    app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")
