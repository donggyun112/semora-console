from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import (
    FakeListChatModel,
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage
from semora import Agent, AgentRuntime, MemorySteps
from semora.orchestrator import Orchestrator
from semora_fork import read_event_checkpoint
from semora_store import MemoryTranscript

from console.fork_demo import (
    EventCheckpointProjector,
    run_from_event,
    run_from_original,
    run_masked_source,
)
from console.scenarios import SYSTEM_PROMPT
from console.units import compose_controls
from console.tools import DemoTools


def session_on(steps, run_id: str = "session:fork"):
    """The session whose steps a fork's effects are, as the console wires it.

    A fork runs under a new run id, so the runtime's per-run record does not span
    it; the session's does. Without one these tools would charge twice.
    """

    async def run(step, fn):
        return await Orchestrator(run_id, steps).run(step, fn)

    return run
from console.units import compose_controls


class BoundFakeListChatModel(FakeListChatModel):
    def bind_tools(self, _tools: Any, **_kwargs: Any) -> Any:
        return self


class BoundFakeMessagesListChatModel(FakeMessagesListChatModel):
    def bind_tools(self, _tools: Any, **_kwargs: Any) -> Any:
        return self


def tool_calling_agent(tools: DemoTools, *responses: AIMessage) -> Agent:
    return Agent(
        "fork-tool-demo",
        "fork tool demo",
        BoundFakeMessagesListChatModel(responses=list(responses)),
        tools,
        SYSTEM_PROMPT,
    )


async def run_tool_source(
    steps: MemorySteps,
    transcript: MemoryTranscript,
    projector: EventCheckpointProjector,
    agent: Agent,
) -> tuple[AgentRuntime, list[dict[str, Any]]]:
    frames: list[dict[str, Any]] = []

    async def emit(event_type: str, payload: dict[str, Any]) -> None:
        frames.append(
            await projector.stamp(
                {"kind": "lifecycle", "type": str(event_type), "payload": payload}
            )
        )

    async def on_event(event: dict[str, Any]) -> None:
        frames.append(await projector.stamp({"kind": "agent", "event": event}))

    runtime = AgentRuntime(store=steps, transcript=transcript, emit=emit)
    await runtime.run(
        "run-source",
        agent,
        prompt="고객을 조회해줘",
        prompt_id="p1",
        conversation_id="conv-tool",
        on_event=on_event,
    )
    return runtime, frames


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
        DemoTools(session=session_on(steps)),
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
        run_id="run-a",
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
    assert [frame["forkable"] for frame in stamped] == [
        True,
        False,
        True,
        False,
        False,
    ]


@pytest.mark.asyncio
async def test_separate_stream_attempts_cannot_reuse_an_event_identity():
    """Resume and recovery streams restart local ordering but must not collide durably."""
    transcript = MemoryTranscript()
    options = {
        "run_id": "run-a",
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
        DemoTools(session=session_on(steps)),
        SYSTEM_PROMPT,
    )
    projector = EventCheckpointProjector(
        transcript,
        run_id="run-source",
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
        DemoTools(session=session_on(steps)),
        SYSTEM_PROMPT,
    )
    projector = EventCheckpointProjector(
        transcript,
        run_id="run-v1",
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
        DemoTools(session=session_on(steps)),
        SYSTEM_PROMPT,
    )
    v1_projector = EventCheckpointProjector(
        transcript,
        run_id="run-v1",
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
        run_id="run-v2",
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


def read_customer_call() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "id": "read-1",
                "name": "read_customer",
                "args": {"customer_id": "c-001"},
                "type": "tool_call",
            }
        ],
    )


def test_recovered_blocked_tool_result_uses_its_top_level_call_id():
    frame = {
        "kind": "agent",
        "event": {
            "type": "tool_call",
            "name": "send_email",
            "blocked": True,
        },
        "call_id": "send-1",
        "checkpoint_phase": "tool_result",
    }

    assert EventCheckpointProjector._call_id(frame) == "send-1"


@pytest.mark.asyncio
async def test_pre_tool_event_fork_reuses_the_pending_call_result_in_the_child_run():
    steps = MemorySteps()
    transcript = MemoryTranscript()
    tools = DemoTools(session=session_on(steps))
    agent = tool_calling_agent(
        tools,
        read_customer_call(),
        AIMessage(content="source finished"),
        AIMessage(content="fork finished"),
    )
    projector = EventCheckpointProjector(
        transcript,
        run_id="run-source",
        conversation_id="conv-tool",
        origin_runs={"p1": "run-source"},
        default_origin_id="p1",
    )
    _, frames = await run_tool_source(steps, transcript, projector, agent)
    pre_tool = next(
        frame
        for frame in frames
        if frame["kind"] == "lifecycle" and frame["type"] == "pre_tool_use"
    )
    capabilities = {
        update["event_id"]: update["restore_edge"]
        for frame in frames
        for update in frame.get("restore_updates", [])
    }

    assert capabilities[pre_tool["event_id"]] == "before"
    checkpoint = await read_event_checkpoint(
        transcript, "conv-tool", pre_tool["event_id"]
    )
    assert checkpoint.before.origin_id is None
    assert checkpoint.before.leaf_uuid is not None

    child_runtime = AgentRuntime(store=steps, transcript=transcript)
    await run_from_event(
        child_runtime,
        steps,
        transcript,
        event_id=pre_tool["event_id"],
        edge="before",
        run_id="run-child",
        conversation_id="conv-tool",
        agent=agent,
        controls=None,
    )

    assert tools.execution_counts["read-1"] == 1
    history = await child_runtime.committed_history("run-child", "conv-tool")
    assert history[-1].content == "fork finished"


@pytest.mark.asyncio
async def test_post_tool_event_fork_reuses_the_result_without_reexecuting_the_effect():
    steps = MemorySteps()
    transcript = MemoryTranscript()
    tools = DemoTools(session=session_on(steps))
    agent = tool_calling_agent(
        tools,
        read_customer_call(),
        AIMessage(content="source finished"),
        AIMessage(content="fork finished"),
    )
    projector = EventCheckpointProjector(
        transcript,
        run_id="run-source",
        conversation_id="conv-tool",
        origin_runs={"p1": "run-source"},
        default_origin_id="p1",
    )
    _, frames = await run_tool_source(steps, transcript, projector, agent)
    post_tool = next(
        frame
        for frame in frames
        if frame["kind"] == "lifecycle" and frame["type"] == "post_tool_use"
    )
    capabilities = {
        update["event_id"]: update["restore_edge"]
        for frame in frames
        for update in frame.get("restore_updates", [])
    }

    assert capabilities[post_tool["event_id"]] == "after"
    checkpoint = await read_event_checkpoint(
        transcript, "conv-tool", post_tool["event_id"]
    )
    assert checkpoint.after.origin_id is None
    assert checkpoint.after.leaf_uuid is not None

    child_runtime = AgentRuntime(store=steps, transcript=transcript)
    await run_from_event(
        child_runtime,
        steps,
        transcript,
        event_id=post_tool["event_id"],
        edge="after",
        run_id="run-child",
        conversation_id="conv-tool",
        agent=agent,
        controls=None,
    )

    assert tools.execution_counts["read-1"] == 1
    history = await child_runtime.committed_history("run-child", "conv-tool")
    assert history[-1].content == "fork finished"


@pytest.mark.asyncio
async def test_parallel_tool_checkpoints_are_stabilized_by_call_id():
    steps = MemorySteps()
    transcript = MemoryTranscript()
    tools = DemoTools(session=session_on(steps))
    calls = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "read-1",
                "name": "read_customer",
                "args": {"customer_id": "c-001"},
                "type": "tool_call",
            },
            {
                "id": "read-2",
                "name": "read_customer",
                "args": {"customer_id": "c-002"},
                "type": "tool_call",
            },
        ],
    )
    agent = tool_calling_agent(tools, calls, AIMessage(content="finished"))
    projector = EventCheckpointProjector(
        transcript,
        run_id="run-source",
        conversation_id="conv-tool",
        origin_runs={"p1": "run-source"},
        default_origin_id="p1",
    )

    _, frames = await run_tool_source(steps, transcript, projector, agent)

    event_call_ids = {
        frame["event_id"]: EventCheckpointProjector._call_id(frame)
        for frame in frames
    }
    stabilized_by = {
        update["event_id"]: (
            EventCheckpointProjector._call_id(frame)
            or str((frame.get("payload") or {}).get("origin_id") or ""),
            update["restore_edge"],
        )
        for frame in frames
        for update in frame.get("restore_updates", [])
    }
    tool_events = {
        event_id: call_id
        for event_id, call_id in event_call_ids.items()
        if call_id in {"read-1", "read-2"}
        and event_id in stabilized_by
    }

    assert set(tool_events.values()) == {"read-1", "read-2"}
    assert all(
        stabilized_by[event_id][0] == call_id
        for event_id, call_id in tool_events.items()
    )
    assert {
        edge for _stabilizer, edge in stabilized_by.values()
    } == {"before", "after"}


@pytest.mark.asyncio
async def test_a_saved_result_carries_the_coordinate_that_makes_it_again():
    """Restoring a result and making it again are two different coordinates.

    A journal policy only speaks while the tool runs, so an operator who turns one on
    at a saved result has nothing to rewrite there. The projector knows which coordinate
    ran the tool and sends it along, which is what the console branches from — the
    client is in no position to work that out from row labels.
    """
    steps = MemorySteps()
    transcript = MemoryTranscript()
    tools = DemoTools(session=session_on(steps))
    agent = tool_calling_agent(
        tools,
        read_customer_call(),
        AIMessage(content="source finished"),
        AIMessage(content="fork finished"),
    )
    projector = EventCheckpointProjector(
        transcript,
        run_id="run-source",
        conversation_id="conv-tool",
        origin_runs={"p1": "run-source"},
        default_origin_id="p1",
    )
    _, frames = await run_tool_source(steps, transcript, projector, agent)

    by_event = {frame["event_id"]: frame for frame in frames if frame.get("event_id")}
    updates = {
        update["event_id"]: update
        for frame in frames
        for update in frame.get("restore_updates", [])
    }
    post_tool = next(
        frame for frame in frames
        if frame["kind"] == "lifecycle" and frame["type"] == "post_tool_use"
    )
    rebuild = updates[post_tool["event_id"]]["rebuild"]
    assert by_event[rebuild["event_id"]]["type"].endswith("pre_tool_use")
    assert rebuild["edge"] == "before"

    before = {
        event_id for event_id, update in updates.items()
        if update.get("rebuild") is None
    }
    assert rebuild["event_id"] in before, "the gate itself has nothing to rebuild"

    child_runtime = AgentRuntime(store=steps, transcript=transcript)
    await run_from_event(
        child_runtime,
        steps,
        transcript,
        event_id=rebuild["event_id"],
        edge=rebuild["edge"],
        run_id="run-child",
        conversation_id="conv-tool",
        agent=agent,
        controls=compose_controls(["pii_mask"]),
    )

    history = await child_runtime.committed_history("run-child", "conv-tool")
    masked = [item for item in history if "***" in str(getattr(item, "content", ""))]
    assert masked, "the tool boundary ran again, so the new journal had its say"
    assert tools.execution_counts["read-1"] == 1, "and the effect still happened once"


def test_a_replayed_result_still_names_the_call_it_answers():
    """A fork replays the call, and the frame's origin id changes shape when it does.

    Fresh, it is the call id; replayed, it reads "tool:<call>:result". Reading only that
    left a forked run with no coordinate before its own tool: the boundary the console
    was assembling never closed, and the trace offered nothing to branch from.
    """
    call = EventCheckpointProjector._tool_result_call_id
    fresh = {"kind": "tool_result", "origin_id": "call-1"}
    replayed = {
        "kind": "tool_result",
        "origin_id": "tool:call-1:result",
        "message": {"data": {"tool_call_id": "call-1"}},
    }
    assert call("lifecycle", "context_injected", fresh) == "call-1"
    assert call("lifecycle", "context_injected", replayed) == "call-1"


@pytest.mark.asyncio
async def test_a_seam_belongs_to_one_run():
    """Replaying a call writes no transcript entry, so a child's leaf is its parent's.

    The console groups a boundary's coordinates by seam. Left as the bare leaf, a child
    row and an inherited parent row grouped together and the branch buttons collapsed
    onto the wrong one.
    """
    steps = MemorySteps()
    transcript = MemoryTranscript()
    tools = DemoTools(session=session_on(steps))
    agent = tool_calling_agent(
        tools, read_customer_call(), AIMessage(content="source finished")
    )
    projector = EventCheckpointProjector(
        transcript,
        run_id="run-source",
        conversation_id="conv-tool",
        origin_runs={"p1": "run-source"},
        default_origin_id="p1",
    )
    _, frames = await run_tool_source(steps, transcript, projector, agent)
    seams = {
        update["seam"]
        for frame in frames
        for update in frame.get("restore_updates", [])
    } | {frame["seam"] for frame in frames if frame.get("seam")}

    assert seams, "the run has boundaries at all"
    assert all(seam.startswith("run-source:") for seam in seams)
