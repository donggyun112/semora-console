"""Native message checkpoints for console execution versions."""

import copy
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic_ai import ModelMessagesTypeAdapter
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from semora.runtime import unanswered_tool_calls
from semora.transcript import marker_entry

from .runtime import ObservedControls

RERUNS = {
    "on_inputs": (
        "on_inputs",
        "before_model",
        "pre_tool_use",
        "post_tool_use",
        "before_finish",
    ),
    "pre_tool_use": ("pre_tool_use", "post_tool_use", "before_model", "before_finish"),
    "post_tool_use": ("post_tool_use", "before_model", "before_finish"),
    "before_model": ("before_model", "pre_tool_use", "post_tool_use", "before_finish"),
}


@dataclass(frozen=True)
class Coordinate:
    from_run_id: str
    history: list[dict[str, Any]]
    boundary: str
    call_id: str | None = None
    origin_id: str | None = None
    prompt: str | None = None


@dataclass(frozen=True)
class EventCheckpoint:
    before: Coordinate
    after: Coordinate


async def read_event_checkpoint(transcript, conversation_id, event_id):
    for entry in reversed(await transcript.read(conversation_id)):
        if (
            entry.get("type") == "console_checkpoint"
            and entry.get("event_id") == event_id
        ):
            coordinate = Coordinate(**entry["coordinate"])
            return EventCheckpoint(coordinate, coordinate)
    raise ValueError(f"no checkpoint for {event_id!r}")


class EventCheckpointProjector:
    def __init__(
        self, transcript, *, run_id, conversation_id, origin_runs, default_origin_id
    ):
        self.transcript = transcript
        self.run_id = run_id
        self.conversation_id = conversation_id
        self.origin_id = default_origin_id
        self.scope = uuid.uuid4().hex
        self.sequence = 0
        self.gates = {}
        self.rebuild = {}
        self.loaded = False

    @staticmethod
    def _call_id(frame):
        return (
            str(
                (frame.get("event") or {}).get("id")
                or (frame.get("payload") or {}).get("call_id")
                or frame.get("call_id")
                or ""
            )
            or None
        )

    @staticmethod
    def _tool_result_call_id(frame_kind, frame_type, payload):
        if (
            frame_kind != "lifecycle"
            or frame_type != "context_injected"
            or payload.get("kind") not in {"tool_result", "resume_result"}
        ):
            return None
        message = payload.get("message") or {}
        return (message.get("data") or message).get("tool_call_id") or payload.get(
            "origin_id"
        )

    async def stamp(self, frame):
        if not self.loaded:
            self.loaded = True
            for entry in await self.transcript.read(self.conversation_id):
                point = entry.get("coordinate") or {}
                if (
                    entry.get("type") == "console_checkpoint"
                    and point.get("from_run_id") == self.run_id
                    and point.get("boundary") == "tool"
                ):
                    call_id = point["call_id"]
                    self.gates.setdefault(call_id, []).append(
                        (entry["event_id"], f"{self.run_id}:tool:{call_id}")
                    )
        frame = copy.deepcopy(frame)
        payload = frame.get("payload") or {}
        coordinate = payload.pop("_checkpoint", None)
        event_id = f"event:{self.scope}:{self.sequence}"
        self.sequence += 1
        updates = []
        boundary = coordinate.get("boundary") if coordinate else None
        call_id = coordinate.get("call_id") if coordinate else self._call_id(frame)
        edge = (
            "before"
            if boundary in {"input", "tool"}
            else "after"
            if boundary == "result"
            else None
        )
        resumes = {
            "input": "on_inputs",
            "tool": "pre_tool_use",
            "result": "before_model",
        }.get(boundary)
        if boundary == "result" and unanswered_tool_calls(
            ModelMessagesTypeAdapter.validate_python(coordinate["history"])
        ):
            resumes = "pre_tool_use"
        seam = f"{self.run_id}:{boundary}:{call_id or event_id}" if boundary else None
        if coordinate:
            await self.transcript.append(
                marker_entry(
                    "console_checkpoint",
                    self.conversation_id,
                    event_id=event_id,
                    coordinate=coordinate,
                )
            )
        if boundary == "tool":
            self.gates.setdefault(call_id, []).append((event_id, seam))
            self.rebuild[call_id] = {
                "event_id": event_id,
                "edge": "before",
                "resumes_at": "pre_tool_use",
                "rejournal_at": "post_tool_use",
            }
        if boundary == "result":
            rejournal_at = (
                "post_tool_use" if payload.get("executed") is not False else None
            )
            for gate_id, gate_seam in self.gates.pop(call_id, []):
                updates.append(
                    {
                        "event_id": gate_id,
                        "restore_edge": "before",
                        "seam": gate_seam,
                        "boundary": "tool",
                        "resumes_at": "pre_tool_use",
                        "rejournal_at": rejournal_at,
                    }
                )
                self.rebuild[call_id] = {
                    "event_id": gate_id,
                    "edge": "before",
                    "resumes_at": "pre_tool_use",
                    "rejournal_at": rejournal_at,
                }
        return {
            **frame,
            "event_id": event_id,
            "fork_origin_id": self.origin_id,
            "forkable": bool(edge) and boundary != "tool",
            "restore_edge": edge,
            "seam": seam,
            "boundary": boundary,
            "resumes_at": resumes,
            "rebuild": self.rebuild.get(call_id) if boundary == "result" else None,
            "restore_updates": updates,
        }


def branch_snapshot(branch, run_id, conversation_id, origin_id, messages):
    shown = []
    for index, message in enumerate(messages):
        content = "".join(
            str(part.content)
            for part in message.parts
            if isinstance(part, (TextPart, UserPromptPart))
        )
        if content:
            shown.append(
                {
                    "id": f"{run_id}:{index}",
                    "role": "assistant"
                    if isinstance(message, ModelResponse)
                    else "user",
                    "content": content,
                }
            )
    return {
        "branch": branch,
        "run_id": run_id,
        "conversation_id": conversation_id,
        "origin_id": origin_id,
        "active": True,
        "messages": shown,
    }


def resume_history(history):
    """Expose remaining batch calls as the final native response Pydantic AI resumes.

    A partial request containing tool returns would otherwise ask the model immediately,
    dropping unanswered siblings. Keep answered pairs in the prefix and move only the
    still-pending calls to the final response, in their original order.
    """
    pending = unanswered_tool_calls(history)
    if not pending or isinstance(history[-1], ModelResponse):
        return history
    pending_ids = {call.tool_call_id for call in pending}
    for index in range(len(history) - 1, -1, -1):
        if isinstance(history[index], ModelResponse):
            response = history[index]
            remaining = copy.deepcopy(response)
            remaining.parts = [
                part
                for part in response.parts
                if isinstance(part, ToolCallPart) and part.tool_call_id in pending_ids
            ]
            response.parts = [
                part
                for part in response.parts
                if not isinstance(part, ToolCallPart)
                or part.tool_call_id not in pending_ids
            ]
            if not response.parts:
                history.pop(index)
            history.append(remaining)
            return history
    return history


async def run_from_event(
    runtime,
    transcript,
    *,
    event_id,
    edge,
    run_id,
    conversation_id,
    agent,
    controls,
    on_event=None,
    aborted=None,
    rejournal=False,
):
    checkpoint = await read_event_checkpoint(transcript, conversation_id, event_id)
    coordinate = checkpoint.before if edge == "before" else checkpoint.after
    history = resume_history(
        list(ModelMessagesTypeAdapter.validate_python(coordinate.history))
    )
    prompt = None
    if coordinate.boundary == "input":
        if history and isinstance(history[-1], ModelRequest):
            history[-1].parts = [
                p for p in history[-1].parts if not isinstance(p, UserPromptPart)
            ]
            if not history[-1].parts:
                history.pop()
        prompt = coordinate.prompt
    observed = ObservedControls(
        runtime,
        run_id,
        controls,
        on_event,
        aborted=aborted,
        prompt_id=coordinate.origin_id,
    )
    # semora copies the source's finished effects into the branch's ledger so they replay,
    # and carries an unreported one over as doubt. Without rejournal the branch's gate is asked
    # about each copied effect first; with it, only the journal sees them.
    result = await runtime.engine.fork(
        coordinate.from_run_id,
        None,
        run_id,
        agent,
        prompt,
        history=history,
        regate=not rejournal,
        conversation_id=conversation_id,
        controls=observed,
    )
    await observed.project_missing(result)
    await runtime.events.publish(
        "branch_snapshot",
        **branch_snapshot(
            "fork",
            run_id,
            conversation_id,
            coordinate.origin_id or event_id,
            result.all_messages(),
        ),
    )
    return {
        "stop_reason": result.stop_reason,
        "text": result.output,
        "output": result.output,
    }
