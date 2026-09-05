import pytest
from pydantic_ai.messages import ToolCallPart
from semora import MemorySteps
from semora_store import MemoryTranscript

from console.provider import DEFAULT_MODEL, openrouter_model
from console.store import (
    FaultInjectingSteps,
    SimulatedWorkerCrash,
    crash_before_approval,
    make_store,
)


def test_provider_raises_without_key(monkeypatch):
    for k in ("OPENROUTER_API_KEY", "OPENROUTER_KEY", "OPEN_ROTURE"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError):
        openrouter_model()


def test_provider_builds_with_key(monkeypatch):
    monkeypatch.delenv("MODEL", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    m = openrouter_model()
    assert m.model_name == DEFAULT_MODEL
    monkeypatch.setenv("MODEL", "vendor/other")
    assert openrouter_model().model_name == "vendor/other"


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
    from semora import Continue

    store = FaultInjectingSteps(MemorySteps())
    store.arm("r1", at="gate")
    stage = crash_before_approval("r1", store)
    call = ToolCallPart("charge_card", {}, "c1")
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
