"""Two-step masking incident used by the console's fork demo."""

from __future__ import annotations

from typing import Any

from nexora_fork import fork_run


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
