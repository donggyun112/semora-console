"""The demo effects, and the session steps that decide whether they happen again.

Nothing here reaches into a dictionary the tools keep. Every property below is the
ledger's, exercised through a real ``Orchestrator``, because a demo whose idempotency
lives beside the runtime proves nothing about the runtime.
"""

import json

import pytest
from semora import MemorySteps
from semora.orchestrator import Orchestrator
from semora_store import Indeterminate

from console.tools import INJECT_NOTE, DemoTools


def session_on(log: MemorySteps, run_id: str = "session:test"):
    """A session step handle: one attempt per effect, as the console builds it."""

    async def run(step, fn):
        return await Orchestrator(run_id, log).run(step, fn)

    return run


def body_of(result: dict) -> dict:
    return json.loads(result["text"])


@pytest.mark.asyncio
async def test_read_customer_carries_pii_and_counts():
    tools = DemoTools()
    result = await tools.execute("read_customer", "id1", {"customer_id": "c-001"})
    assert "@" in result["text"] and "-" in result["text"]
    assert result["execution_count"] == 1


@pytest.mark.asyncio
async def test_c_inj_carries_hidden_directive_without_pii():
    tools = DemoTools()
    result = await tools.execute("read_customer", "id-inj", {"customer_id": "c-inj"})
    assert INJECT_NOTE in result["text"]
    assert "@" not in result["text"] and "ssn" not in result["text"].lower()


@pytest.mark.asyncio
async def test_without_a_session_a_tool_simply_acts():
    """No ledger, no guarantee — the same thing the runtime says about running storeless."""
    tools = DemoTools()
    await tools.execute("charge_card", "c1", {"customer_id": "c-001", "amount": "49"})
    again = await tools.execute("charge_card", "c2", {"customer_id": "c-001", "amount": "49"})
    assert again["idempotency"] == {"key": "charge:c-001", "replayed": False}
    assert tools.execution_counts == {"c1": 1, "c2": 1}, "the same card, charged twice"


@pytest.mark.asyncio
async def test_a_charge_is_a_session_step_so_a_second_call_replays_it():
    """The business key spans calls: whether this customer was charged, not this call."""
    log = MemorySteps()
    tools = DemoTools(session=session_on(log))

    first = await tools.execute("charge_card", "c1", {"customer_id": "c-001", "amount": "49"})
    assert first["idempotency"] == {"key": "charge:c-001", "replayed": False}
    assert first["execution_count"] == 1

    again = await tools.execute("charge_card", "c2", {"customer_id": "c-001", "amount": "49"})
    assert again["idempotency"] == {"key": "charge:c-001", "replayed": True}
    assert again["execution_count"] == 1, "the recorded count, not a second charge"
    assert tools.execution_counts == {"c1": 1}, "only one call ever performed"


@pytest.mark.asyncio
async def test_the_same_call_replays_whole_rather_than_running_again():
    log = MemorySteps()
    tools = DemoTools(session=session_on(log))

    first = await tools.execute("read_customer", "r1", {"customer_id": "c-001"})
    assert first["execution"] == {"call_id": "r1", "replayed": False}

    again = await tools.execute("read_customer", "r1", {"customer_id": "c-001"})
    assert again["execution"] == {"call_id": "r1", "replayed": True}
    assert body_of(again) == body_of(first)


@pytest.mark.asyncio
async def test_a_second_agent_run_in_one_session_does_not_charge_twice():
    """A rerun is a new agent run and the same session, which is the whole point."""
    log = MemorySteps()
    charged = []
    for call_id in ("run1-c1", "run2-c1"):
        tools = DemoTools(session=session_on(log))
        charged.append(await tools.execute(
            "charge_card", call_id, {"customer_id": "c-001", "amount": "49"}
        ))
    assert [row["idempotency"]["replayed"] for row in charged] == [False, True]


@pytest.mark.asyncio
async def test_separate_sessions_do_not_replay_each_others_charges():
    log = MemorySteps()
    alice = DemoTools(session=session_on(log, "session:alice"))
    bob = DemoTools(session=session_on(log, "session:bob"))

    await alice.execute("charge_card", "a1", {"customer_id": "c-001", "amount": "49"})
    theirs = await bob.execute("charge_card", "b1", {"customer_id": "c-001", "amount": "49"})
    assert theirs["idempotency"]["replayed"] is False


@pytest.mark.asyncio
async def test_the_same_customer_at_a_different_amount_is_refused():
    log = MemorySteps()
    tools = DemoTools(session=session_on(log))
    await tools.execute("charge_card", "c1", {"customer_id": "c-001", "amount": "49"})

    same = await tools.execute("charge_card", "c2", {"customer_id": "c-001", "amount": "49.00"})
    assert same["idempotency"]["replayed"] is True, "the same charge, written differently"

    changed = await tools.execute("charge_card", "c3", {"customer_id": "c-001", "amount": "51"})
    assert changed["code"] == "payment_record_conflict"


@pytest.mark.asyncio
async def test_the_model_dressing_the_amount_differently_is_the_same_charge():
    """The amount comes from an LLM, which writes 49, "49.00" and "$49" for one charge.

    Comparing what it typed rather than what it meant let a single formatting change
    lock a customer out of ever being charged again in that session.
    """
    log = MemorySteps()
    tools = DemoTools(session=session_on(log, "session:money"))
    await tools.execute("charge_card", "c1", {"customer_id": "c-001", "amount": "$49"})

    for written in ("49", "49.00", "USD 49"):
        again = await tools.execute(
            "charge_card", f"c-{written}", {"customer_id": "c-001", "amount": written}
        )
        assert again["idempotency"]["replayed"] is True, written

    changed = await tools.execute("charge_card", "c9", {"customer_id": "c-001", "amount": "51"})
    assert changed["code"] == "payment_record_conflict", "a real difference still refuses"


@pytest.mark.asyncio
async def test_a_worker_that_died_mid_charge_leaves_the_step_undecided():
    """The ledger holds an effect it cannot vouch for, and says so instead of repeating it."""
    log = MemorySteps()
    orchestrator = Orchestrator("session:crash", log)

    async def charge_then_vanish():
        raise __import__("asyncio").CancelledError()

    with pytest.raises(__import__("asyncio").CancelledError):
        await orchestrator.run("charge:c-001", charge_then_vanish)

    tools = DemoTools(session=session_on(log, "session:crash"))
    with pytest.raises(Indeterminate):
        await tools.execute("charge_card", "c9", {"customer_id": "c-001", "amount": "49"})


@pytest.mark.asyncio
async def test_charging_one_customer_twice_in_one_attempt_is_told_to_the_model():
    """Two steps sharing a name would replay as one another, so the session refuses.

    Surfaced as the tool's answer: asking twice in one turn is the model's mistake to
    hear about, not the console's to hide behind a 500.
    """
    log = MemorySteps()
    orchestrator = Orchestrator("session:dup", log)

    async def one_attempt(step, fn):
        return await orchestrator.run(step, fn)

    tools = DemoTools(session=one_attempt)
    await tools.execute("charge_card", "c1", {"customer_id": "c-001", "amount": "49"})
    twice = await tools.execute("charge_card", "c2", {"customer_id": "c-001", "amount": "49"})
    assert twice["code"] == "duplicate_effect"


@pytest.mark.asyncio
async def test_non_payment_tools_answer_and_count():
    tools = DemoTools()
    note = await tools.execute("remember_note", "n1", {"key": "deploy", "value": "ready"})
    assert body_of(note) == {"key": "deploy", "status": "ok"}
    assert tools.notes == {"deploy": "ready"}

    sent = await tools.execute("send_email", "e1", {"to": "ops@acme.io", "body": "hi"})
    assert body_of(sent) == {"status": "sent", "to": "ops@acme.io"}

    unknown = await tools.execute("nope", "u1", {})
    assert unknown["type"] == "error"


def test_charge_card_requires_the_customer_it_charges():
    """The idempotency key is the customer, so the model may not leave it out."""
    charge = DemoTools().get("charge_card")
    assert charge is not None
    assert charge["parameters"]["required"] == ["customer_id", "amount"]
