from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from nexora import Agent, AgentRuntime, MemorySteps
from nexora_fork import read_event_checkpoint
from nexora_store import MemoryTranscript

from console.fork_demo import (
    EventCheckpointProjector,
    run_from_event,
    run_from_original,
    run_masked_source,
)
from console.scenarios import SYSTEM_PROMPT
from console.tools import DemoTools
from console.units import compose_controls


class BoundFakeListChatModel(FakeListChatModel):
    def bind_tools(self, _tools: Any, **_kwargs: Any) -> Any:
        return self


@pytest.mark.asyncio
async def test_fork_restores_original_and_preserves_masked_source():
    steps = MemorySteps()
    transcript = MemoryTranscript()
    snapshots: list[dict[str, Any]] = []

    async def emit(event_type: str, payload: dict[str, Any]) -> None:
        if str(event_type) == "branch_snapshot":
            snapshots.append(payload)

    runtime = AgentRuntime(store=steps, transcript=transcript, emit=emit)
    agent = Agent(
        "fork-demo",
        "fork demo",
        BoundFakeListChatModel(
            responses=["hello", "masked response", "original response"]
        ),
        DemoTools(),
        SYSTEM_PROMPT,
    )

    await run_masked_source(
        runtime,
        run_id="run-b",
        prefix_run_id="run-a",
        conversation_id="conv",
        origin_id="p2",
        prompt="ssn is 123-45",
        agent=agent,
        controls=compose_controls(["input_mask"]),
    )
    await run_from_original(
        runtime,
        steps,
        from_run_id="run-b",
        run_id="run-c",
        conversation_id="conv",
        origin_id="p2",
        agent=agent,
        controls=None,
    )

    assert "ssn is ***" in [message["content"] for message in snapshots[0]["messages"]]
    assert "ssn is 123-45" in [
        message["content"] for message in snapshots[1]["messages"]
    ]
    source = next(
        record for record in await steps.list_inputs("run-b") if record.input_id == "p2"
    )
    assert "123-45" in str(source.value)
    assert [record.input_id for record in await steps.list_inputs("run-c")] == ["p2"]

    head = await runtime.committed_history("run-c", "conv")
    assert [message.content for message in head] == [
        "대화를 시작하고 hello라고 답해줘.",
        "hello",
        "ssn is 123-45",
        "original response",
    ]


@pytest.mark.asyncio
async def test_every_projected_event_gets_a_durable_fork_coordinate():
    """Deleting frame stamping must leave at least one visible row without a checkpoint."""
    transcript = MemoryTranscript()
    projector = EventCheckpointProjector(
        transcript,
        conversation_id="conv",
        origin_runs={"p1": "run-a", "p2": "run-b"},
        default_origin_id="p1",
    )
    frames = [
        {
            "kind": "lifecycle",
            "type": "context_injected",
            "payload": {"origin_id": "p1", "kind": "user_prompt"},
        },
        {"kind": "agent", "event": {"type": "text", "text": "hello"}},
        {
            "kind": "lifecycle",
            "type": "context_injected",
            "payload": {"origin_id": "p2", "kind": "user_prompt"},
        },
        {"kind": "unit", "unit": "input_mask", "verdict": "rewrite"},
        {"kind": "outcome", "outcome": {"stop_reason": "completed"}},
    ]

    stamped = [await projector.stamp(frame) for frame in frames]

    event_ids = [frame["event_id"] for frame in stamped]
    assert len(set(event_ids)) == len(frames)
    checkpoints = [
        await read_event_checkpoint(transcript, "conv", event_id)
        for event_id in event_ids
    ]
    assert [checkpoint.before.origin_id for checkpoint in checkpoints] == [
        "p1",
        "p1",
        "p2",
        "p2",
        "p2",
    ]
    assert [checkpoint.before.from_run_id for checkpoint in checkpoints] == [
        "run-a",
        "run-a",
        "run-b",
        "run-b",
        "run-b",
    ]


@pytest.mark.asyncio
async def test_separate_stream_attempts_cannot_reuse_an_event_identity():
    """Resume and recovery streams restart local ordering but must not collide durably."""
    transcript = MemoryTranscript()
    options = {
        "conversation_id": "conv",
        "origin_runs": {"p1": "run-a"},
        "default_origin_id": "p1",
    }
    first = EventCheckpointProjector(transcript, **options)
    second = EventCheckpointProjector(transcript, **options)
    frame = {"kind": "lifecycle", "type": "session_start", "payload": {}}

    first_event = await first.stamp(frame)
    second_event = await second.stamp(frame)

    assert first_event["event_id"] != second_event["event_id"]


@pytest.mark.asyncio
async def test_an_early_event_replays_the_real_prompt_after_the_source_completes():
    """A checkpoint emitted before admission must route to the prompt that later entered context."""
    steps = MemorySteps()
    transcript = MemoryTranscript()
    runtime = AgentRuntime(store=steps, transcript=transcript)
    agent = Agent(
        "fork-demo",
        "fork demo",
        BoundFakeListChatModel(responses=["source response", "fork response"]),
        DemoTools(),
        SYSTEM_PROMPT,
    )
    projector = EventCheckpointProjector(
        transcript,
        conversation_id="conv-early",
        origin_runs={"prompt-real": "run-source"},
        default_origin_id="prompt-real",
    )
    early = await projector.stamp({"kind": "meta", "run_id": "run-source"})

    await runtime.run(
        "run-source",
        agent,
        prompt="실제 프롬프트",
        prompt_id="prompt-real",
        conversation_id="conv-early",
    )
    await run_from_event(
        runtime,
        steps,
        transcript,
        event_id=early["event_id"],
        edge="before",
        run_id="run-fork",
        conversation_id="conv-early",
        agent=agent,
        controls=None,
    )

    history = await runtime.committed_history("run-fork", "conv-early")
    assert [message.content for message in history] == ["실제 프롬프트", "fork response"]


@pytest.mark.asyncio
async def test_event_fork_applies_the_new_controls_to_the_replayed_input():
    steps = MemorySteps()
    transcript = MemoryTranscript()
    runtime = AgentRuntime(store=steps, transcript=transcript)
    agent = Agent(
        "fork-demo",
        "fork demo",
        BoundFakeListChatModel(responses=["source response", "masked response"]),
        DemoTools(),
        SYSTEM_PROMPT,
    )
    projector = EventCheckpointProjector(
        transcript,
        conversation_id="conv-policy",
        origin_runs={"p1": "run-v1"},
        default_origin_id="p1",
    )

    await runtime.run(
        "run-v1",
        agent,
        prompt="ssn is 123-45",
        prompt_id="p1",
        conversation_id="conv-policy",
    )
    source_event = await projector.stamp({"kind": "outcome"})
    await run_from_event(
        runtime,
        steps,
        transcript,
        event_id=source_event["event_id"],
        edge="before",
        run_id="run-v2",
        conversation_id="conv-policy",
        agent=agent,
        controls=compose_controls(["input_mask"]),
    )

    history = await runtime.committed_history("run-v2", "conv-policy")
    contents = [message.content for message in history]
    assert "ssn is ***" in contents
    assert "ssn is 123-45" not in contents


@pytest.mark.asyncio
async def test_a_fork_event_can_be_versioned_again_from_the_child_run():
    steps = MemorySteps()
    transcript = MemoryTranscript()
    runtime = AgentRuntime(store=steps, transcript=transcript)
    agent = Agent(
        "fork-demo",
        "fork demo",
        BoundFakeListChatModel(responses=["v1 response", "v2 response", "v3 response"]),
        DemoTools(),
        SYSTEM_PROMPT,
    )
    v1_projector = EventCheckpointProjector(
        transcript,
        conversation_id="conv-lineage",
        origin_runs={"p1": "run-v1"},
        default_origin_id="p1",
    )

    await runtime.run(
        "run-v1",
        agent,
        prompt="version me",
        prompt_id="p1",
        conversation_id="conv-lineage",
    )
    v1_event = await v1_projector.stamp({"kind": "outcome"})
    await run_from_event(
        runtime,
        steps,
        transcript,
        event_id=v1_event["event_id"],
        edge="before",
        run_id="run-v2",
        conversation_id="conv-lineage",
        agent=agent,
        controls=None,
    )

    v2_projector = EventCheckpointProjector(
        transcript,
        conversation_id="conv-lineage",
        origin_runs={"p1": "run-v2"},
        default_origin_id="p1",
    )
    v2_event = await v2_projector.stamp({"kind": "outcome"})
    checkpoint = await read_event_checkpoint(
        transcript, "conv-lineage", v2_event["event_id"]
    )
    assert checkpoint.before.from_run_id == "run-v2"

    await run_from_event(
        runtime,
        steps,
        transcript,
        event_id=v2_event["event_id"],
        edge="before",
        run_id="run-v3",
        conversation_id="conv-lineage",
        agent=agent,
        controls=None,
    )

    history = await runtime.committed_history("run-v3", "conv-lineage")
    assert [message.content for message in history] == ["version me", "v3 response"]
