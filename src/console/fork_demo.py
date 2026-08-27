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
        run_id: str,
        conversation_id: str,
        origin_runs: dict[str, str],
        default_origin_id: str | None,
    ) -> None:
        self._transcript = transcript
        self._run_id = run_id
        self._conversation_id = conversation_id
        self._origin_runs = dict(origin_runs)
        self._origin_id = default_origin_id
        self._last_leaf: str | None = None
        self._before_tool_events: dict[str, list[tuple[str, str]]] = {}
        self._after_tool_events: dict[str, list[tuple[str, str]]] = {}
        self._scope = uuid.uuid4().hex
        self._sequence = 0

    def _coordinate(self, origin_id: str | None, leaf_uuid: str | None) -> ForkCoordinate:
        from_run_id = self._origin_runs.get(origin_id, self._run_id)
        return ForkCoordinate(from_run_id, origin_id, leaf_uuid)

    @staticmethod
    def _call_id(frame: dict[str, Any]) -> str | None:
        if frame.get("kind") == "agent":
            event = frame.get("event") or {}
            value = event.get("id") if isinstance(event, dict) else None
            value = value or frame.get("call_id")
            return str(value) if value else None
        if frame.get("kind") == "lifecycle":
            payload = frame.get("payload") or {}
            value = payload.get("call_id") if isinstance(payload, dict) else None
            return str(value) if value else None
        value = frame.get("call_id")
        return str(value) if value else None

    async def _stabilize(
        self,
        event_points: list[tuple[str, str]],
        *,
        leaf_uuid: str | None,
    ) -> list[dict[str, str]]:
        if leaf_uuid is None:
            return []
        coordinate = ForkCoordinate(self._run_id, None, leaf_uuid)
        updates: list[dict[str, str]] = []
        for event_id, edge in event_points:
            await record_event_checkpoint(
                self._transcript,
                EventCheckpoint(
                    event_id,
                    self._conversation_id,
                    coordinate,
                    coordinate,
                ),
            )
            updates.append({"event_id": event_id, "restore_edge": edge})
        return updates

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
        call_id = self._call_id(frame)
        frame_kind = str(frame.get("kind") or "")
        frame_type = str(frame.get("type") or "")
        event = frame.get("event") if frame_kind == "agent" else None
        agent_type = str(event.get("type") or "") if isinstance(event, dict) else ""
        checkpoint_phase = str(frame.get("checkpoint_phase") or "")
        restore_updates: list[dict[str, str]] = []

        if (
            call_id
            and frame_kind == "agent"
            and agent_type == "tool_call"
            and checkpoint_phase != "tool_result"
        ):
            self._before_tool_events.setdefault(call_id, []).append((event_id, "after"))
        if call_id and frame_kind == "lifecycle" and frame_type.endswith("pre_tool_use"):
            self._before_tool_events.setdefault(call_id, []).append((event_id, "before"))

        is_tool_result = frame_kind == "agent" and (
            agent_type == "tool_result" or checkpoint_phase == "tool_result"
        )
        if call_id and is_tool_result:
            restore_updates.extend(
                await self._stabilize(
                    self._before_tool_events.pop(call_id, []),
                    leaf_uuid=current_leaf,
                )
            )
            self._after_tool_events.setdefault(call_id, []).append((event_id, "after"))

        if call_id and frame_kind == "lifecycle" and frame_type.endswith(
            ("post_tool_use", "post_tool_use_failure")
        ):
            self._after_tool_events.setdefault(call_id, []).append((event_id, "after"))

        payload_kind = payload.get("kind") if isinstance(payload, dict) else None
        tool_result_origin = (
            str(payload.get("origin_id"))
            if frame_kind == "lifecycle"
            and frame_type.endswith("context_injected")
            and payload_kind == "tool_result"
            and payload.get("origin_id")
            else None
        )
        if tool_result_origin:
            restore_updates.extend(
                await self._stabilize(
                    self._after_tool_events.pop(tool_result_origin, []),
                    leaf_uuid=current_leaf,
                )
            )

        self._last_leaf = current_leaf
        input_fork = (
            frame.get("kind") == "lifecycle"
            and frame_type.endswith("context_injected")
            and payload_kind == "user_prompt"
            and candidate in self._origin_runs
        )
        tool_result_fork = bool(tool_result_origin and current_leaf)
        restore_edge = "before" if input_fork else "after" if tool_result_fork else None
        return {
            **frame,
            "event_id": event_id,
            "fork_origin_id": before.origin_id,
            "forkable": bool(restore_edge),
            "restore_edge": restore_edge,
            "restore_updates": restore_updates,
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
