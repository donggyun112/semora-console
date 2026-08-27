"""Two-step masking incident used by the console's fork demo."""

from __future__ import annotations

import uuid
from typing import Any

from nexora.transcript import active_branch
from nexora_fork import (
    EventCheckpoint,
    ForkCoordinate,
    fork_event,
    fork_run,
    read_event_checkpoint,
    record_event_checkpoint,
)


class EventCheckpointProjector:
    """Attach every visible frame to the nearest durable input/transcript coordinate."""

    def __init__(
        self,
        transcript: Any,
        *,
        conversation_id: str,
        origin_runs: dict[str, str],
        default_origin_id: str,
    ) -> None:
        self._transcript = transcript
        self._conversation_id = conversation_id
        self._origin_runs = dict(origin_runs)
        self._origin_id = default_origin_id
        self._last_leaf: str | None = None
        self._scope = uuid.uuid4().hex
        self._sequence = 0

    def _coordinate(self, origin_id: str, leaf_uuid: str | None) -> ForkCoordinate:
        return ForkCoordinate(self._origin_runs[origin_id], origin_id, leaf_uuid)

    @staticmethod
    def _input_origin(frame: dict[str, Any]) -> str | None:
        if frame.get("kind") != "lifecycle":
            return None
        payload = frame.get("payload") or {}
        if frame.get("type") == "user_prompt_submit":
            value = payload.get("input_id")
            return str(value) if value else None
        if frame.get("type") == "context_injected":
            value = payload.get("origin_id")
            return str(value) if value else None
        return None

    async def stamp(self, frame: dict[str, Any]) -> dict[str, Any]:
        """Persist and return one uniquely identified visible frame."""
        entries = await self._transcript.read(self._conversation_id)
        branch = active_branch(entries)
        current_leaf = str(branch[-1]["uuid"]) if branch else None
        candidate = self._input_origin(frame)
        before_origin = self._origin_id
        if frame.get("type") == "context_injected" and candidate in self._origin_runs:
            before_origin = str(candidate)
        before = self._coordinate(before_origin, self._last_leaf)
        if candidate in self._origin_runs:
            self._origin_id = str(candidate)
        after = self._coordinate(self._origin_id, current_leaf)

        payload = frame.get("payload") or {}
        logical = payload.get("event_id") if isinstance(payload, dict) else None
        base = str(logical or frame.get("event_id") or "event")
        event_id = f"{base}:{self._scope}:{self._sequence}"
        self._sequence += 1
        await record_event_checkpoint(
            self._transcript,
            EventCheckpoint(event_id, self._conversation_id, before, after),
        )
        self._last_leaf = current_leaf
        return {
            **frame,
            "event_id": event_id,
            "fork_origin_id": before.origin_id,
        }


def branch_snapshot(
    branch: str,
    run_id: str,
    conversation_id: str,
    origin_id: str,
    messages: list[Any],
) -> dict[str, Any]:
    """Project the active transcript branch into a UI-safe lifecycle payload."""
    return {
        "branch": branch,
        "run_id": run_id,
        "conversation_id": conversation_id,
        "origin_id": origin_id,
        "active": True,
        "messages": [
            {
                "id": str(message.id or ""),
                "role": "user" if message.type == "human" else "assistant",
                "content": str(message.content),
            }
            for message in messages
            if message.type in {"human", "ai"}
        ],
    }


async def run_masked_source(
    runtime: Any,
    *,
    run_id: str,
    prefix_run_id: str,
    prefix_origin_id: str | None = None,
    conversation_id: str,
    origin_id: str,
    prompt: str,
    agent: Any,
    controls: Any,
    on_event: Any = None,
    should_stop_after_turn: Any = None,
    aborted: Any = None,
) -> dict[str, Any]:
    """Create the shared prefix, then commit the masked source branch."""
    options = {
        name: value
        for name, value in {
            "should_stop_after_turn": should_stop_after_turn,
            "aborted": aborted,
        }.items()
        if value is not None
    }
    await runtime.run(
        prefix_run_id,
        agent,
        prompt="대화를 시작하고 hello라고 답해줘.",
        prompt_id=prefix_origin_id,
        conversation_id=conversation_id,
        **options,
    )
    outcome = await runtime.run(
        run_id,
        agent,
        prompt=prompt,
        prompt_id=origin_id,
        controls=controls,
        conversation_id=conversation_id,
        on_event=on_event,
        **options,
    )
    history = await runtime.committed_history(run_id, conversation_id)
    snapshot = branch_snapshot("source", run_id, conversation_id, origin_id, history)
    await runtime.events.publish("branch_snapshot", **snapshot)
    return outcome


async def run_from_original(
    runtime: Any,
    store: Any,
    *,
    from_run_id: str,
    run_id: str,
    conversation_id: str,
    origin_id: str,
    agent: Any,
    controls: Any,
    on_event: Any = None,
    should_stop_after_turn: Any = None,
    aborted: Any = None,
) -> dict[str, Any]:
    """Fork at ``origin_id`` and pass the ledger original through the new controls."""
    options = {
        name: value
        for name, value in {
            "should_stop_after_turn": should_stop_after_turn,
            "aborted": aborted,
        }.items()
        if value is not None
    }
    outcome = await fork_run(
        runtime,
        store,
        from_run_id=from_run_id,
        origin_id=origin_id,
        run_id=run_id,
        model=agent,
        controls=controls,
        conversation_id=conversation_id,
        on_event=on_event,
        **options,
    )
    history = await runtime.committed_history(run_id, conversation_id)
    snapshot = branch_snapshot("fork", run_id, conversation_id, origin_id, history)
    await runtime.events.publish("branch_snapshot", **snapshot)
    return outcome


async def run_from_event(
    runtime: Any,
    store: Any,
    transcript: Any,
    *,
    event_id: str,
    edge: str,
    run_id: str,
    conversation_id: str,
    agent: Any,
    controls: Any,
    on_event: Any = None,
    should_stop_after_turn: Any = None,
    aborted: Any = None,
) -> dict[str, Any]:
    """Fork one selected observation edge and publish the resulting active branch."""
    options = {
        name: value
        for name, value in {
            "on_event": on_event,
            "should_stop_after_turn": should_stop_after_turn,
            "aborted": aborted,
        }.items()
        if value is not None
    }
    checkpoint = await read_event_checkpoint(transcript, conversation_id, event_id)
    coordinate = checkpoint.before if edge == "before" else checkpoint.after
    outcome = await fork_event(
        runtime,
        store,
        transcript,
        event_id=event_id,
        edge=edge,
        run_id=run_id,
        model=agent,
        controls=controls,
        conversation_id=conversation_id,
        **options,
    )
    history = await runtime.committed_history(run_id, conversation_id)
    snapshot = branch_snapshot(
        "fork",
        run_id,
        conversation_id,
        coordinate.origin_id or event_id,
        history,
    )
    await runtime.events.publish("branch_snapshot", **snapshot)
    return outcome
