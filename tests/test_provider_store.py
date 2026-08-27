import pytest
from langchain_core.messages import AIMessage
from nexora import AgentRuntime, MemorySteps
from nexora.orchestrator import AgentSuspended, Orchestrator
from nexora_store import MemoryTranscript

from console.provider import DEFAULT_MODEL, openrouter_model
from console.store import FaultInjectingSteps, SimulatedWorkerCrash, crash_before_approval, make_store
from console.tools import DemoTools
from console.units import compose_controls


def test_provider_raises_without_key(monkeypatch):
    for k in ("OPENROUTER_API_KEY", "OPENROUTER_KEY", "OPEN_ROTURE"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError):
        openrouter_model()


def test_provider_builds_with_key(monkeypatch):
    monkeypatch.delenv("MODEL", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    m = openrouter_model()
    assert DEFAULT_MODEL.startswith("deepseek/")
    assert m.model_name == DEFAULT_MODEL


@pytest.mark.asyncio
async def test_make_store_memory_without_db(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    store, transcript, closer = await make_store()
    assert isinstance(store, FaultInjectingSteps)
    assert isinstance(transcript, MemoryTranscript)
    assert closer is None


@pytest.mark.asyncio
async def test_fault_store_crashes_after_tool_commit():
    """A committed tool step stays done; the crash is the worker, not the effect."""
    store = FaultInjectingSteps(MemorySteps())
    store.arm("r1")
    token = await store.acquire("r1", "local", 60)
    assert token
    assert await store.start("r1", "c1", token) is True
    try:
        await store.finish_effect("r1", "c1", {"ok": True}, token)
        raise AssertionError("expected SimulatedWorkerCrash")
    except SimulatedWorkerCrash as crashed:
        assert crashed.step == "c1"
    step = await store.read("r1", "c1")
    assert step.status == "done"


@pytest.mark.asyncio
async def test_gate_arm_does_not_crash_on_commit():
    """A pre-approval crash is the gate, not finish_effect."""
    store = FaultInjectingSteps(MemorySteps())
    store.arm("r1", at="gate")
    token = await store.acquire("r1", "local", 60)
    assert await store.start("r1", "c1", token) is True
    await store.finish_effect("r1", "c1", {"ok": True}, token)
    assert (await store.read("r1", "c1")).status == "done"


@pytest.mark.asyncio
async def test_gate_hook_fires_once_then_continues():
    """consume_gate is one-shot so recover can re-run pre_tool_use."""
    from nexora import Continue

    store = FaultInjectingSteps(MemorySteps())
    store.arm("r1", at="gate")
    stage = crash_before_approval("r1", store)
    call = {"id": "c1", "name": "charge_card", "args": {}, "type": "tool_call"}
    try:
        await stage(None, call)
        raise AssertionError("expected SimulatedWorkerCrash")
    except SimulatedWorkerCrash as crashed:
        assert crashed.step == "c1"
    assert isinstance(await stage(None, call), Continue)


@pytest.mark.asyncio
async def test_fault_survives_for_execution_scope():
    """Runtime scopes the ledger; the crash hook must stay on that object."""
    store = FaultInjectingSteps(MemorySteps())
    store.arm("r1")
    scoped = store.for_execution(object())
    token = await scoped.acquire("r1", "local", 60)
    assert await scoped.start("r1", "call_1", token) is True
    try:
        await scoped.finish_effect("r1", "call_1", {"ok": True}, token)
        raise AssertionError("expected SimulatedWorkerCrash")
    except SimulatedWorkerCrash:
        pass
    assert (await scoped.read("r1", "call_1")).status == "done"


@pytest.mark.asyncio
async def test_recover_after_pre_park_crash_reissues_approval_under_the_same_id():
    """tool_call is in history, park is not; recover re-gates and Suspends with that call id."""
    store = FaultInjectingSteps(MemorySteps())
    run_id = "run-pre-park"
    store.arm(run_id, at="gate")
    call = {
        "id": "call-charge",
        "name": "charge_card",
        "args": {"customer_id": "c-001", "amount": "49"},
        "type": "tool_call",
    }
    calls = [call]
    history = [AIMessage(content="", tool_calls=calls)]
    tools = DemoTools()
    plane = compose_controls(["approval"], extra_pre=[crash_before_approval(run_id, store)])

    async with Orchestrator(run_id, store) as owner:
        await owner.record_pending(calls, 0)
        with pytest.raises(SimulatedWorkerCrash) as crashed:
            await owner.execute_round(tools, calls, lambda: False, controls=plane)
        assert crashed.value.step == "call-charge"
    assert tools.execution_counts == {}

    with pytest.raises(AgentSuspended) as stopped:
        await AgentRuntime(store=store).recover(
            run_id,
            history,
            object(),
            DemoTools(),
            controls=compose_controls(["approval"]),
            retry_running=False,
        )
    assert stopped.value.pending_id == "call-charge"
    assert stopped.value.tool_call_id == "call-charge"
    assert stopped.value.pending == [("call-charge", "call-charge")]


@pytest.mark.asyncio
async def test_recover_parallel_round_finishes_each_call_exactly_once():
    """A committed call is replayed while absent siblings execute once after recovery."""
    store = FaultInjectingSteps(MemorySteps())
    run_id = "run-parallel-crash"
    calls = [
        {
            "id": "charge-c001",
            "name": "charge_card",
            "args": {"customer_id": "c-001", "amount": "10"},
            "type": "tool_call",
        },
        {
            "id": "charge-c002",
            "name": "charge_card",
            "args": {"customer_id": "c-002", "amount": "10"},
            "type": "tool_call",
        },
        {
            "id": "charge-c003",
            "name": "charge_card",
            "args": {"customer_id": "c-003", "amount": "10"},
            "type": "tool_call",
        },
    ]
    history = [AIMessage(content="", tool_calls=calls)]
    tools = DemoTools()
    store.arm(run_id)

    async with Orchestrator(run_id, store) as owner:
        with pytest.raises(SimulatedWorkerCrash) as crashed:
            await owner.execute_round(tools, calls, lambda: False)
    assert crashed.value.step == "charge-c001"
    assert tools.execution_counts == {"charge-c001": 1}

    async with Orchestrator(run_id, store) as owner:
        recovered = await owner.recover_pending(
            history,
            tools,
            retry_running=False,
        )

    assert len(recovered.completed) == 3
    assert tools.execution_counts == {
        "charge-c001": 1,
        "charge-c002": 1,
        "charge-c003": 1,
    }
