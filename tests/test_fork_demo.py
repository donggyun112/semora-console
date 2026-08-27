from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from nexora import Agent, AgentRuntime, MemorySteps
from nexora_store import MemoryTranscript

from console.fork_demo import run_from_original, run_masked_source
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
