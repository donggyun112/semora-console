import json

import pytest

from console.tools import DemoTools, reset_payment_ledgers


@pytest.mark.asyncio
async def test_read_customer_carries_pii_and_counts():
    t = DemoTools()
    r = await t.execute("read_customer", "id1", {"customer_id": "c-001"})
    assert "@" in r["text"] and "-" in r["text"]
    assert r["execution_count"] == 1


@pytest.mark.asyncio
async def test_c_inj_carries_hidden_directive_without_pii():
    t = DemoTools()
    r = await t.execute("read_customer", "id-inj", {"customer_id": "c-inj"})
    assert "9999" in r["text"] and "charge_card" in r["text"]
    assert "@" not in r["text"] and "ssn" not in r["text"].lower()


@pytest.mark.asyncio
async def test_same_call_id_replays_every_tool_result():
    t = DemoTools(payment_batch_id="batch-1")
    first = await t.execute("send_email", "send-1", {"to": "first@example.com"})
    replayed = await t.execute("send_email", "send-1", {"to": "first@example.com"})

    assert replayed["text"] == first["text"]
    assert replayed["execution"] == {"call_id": "send-1", "replayed": True}
    assert t.execution_counts == {"send-1": 1}


@pytest.mark.asyncio
async def test_same_call_id_rejects_a_different_invocation():
    t = DemoTools(payment_batch_id="batch-1")
    await t.execute("send_email", "send-1", {"to": "first@example.com"})
    conflict = await t.execute("send_email", "send-1", {"to": "second@example.com"})

    assert conflict["type"] == "error"
    assert conflict["code"] == "call_id_conflict"
    assert t.execution_counts == {"send-1": 1}


@pytest.mark.asyncio
async def test_charge_card_reuses_a_payment_record_across_new_call_ids():
    t = DemoTools(payment_batch_id="batch-1")
    first = await t.execute(
        "charge_card", "charge-1", {"customer_id": "c-001", "amount": "10"}
    )
    replayed = await t.execute(
        "charge_card", "charge-2", {"customer_id": "c-001", "amount": "10"}
    )

    assert json.loads(first["text"]) == {"status": "charged", "amount": "10"}
    assert first["idempotency"] == {"key": "batch-1:c-001", "replayed": False}
    assert replayed["text"] == first["text"]
    assert replayed["execution_count"] == 1
    assert replayed["execution"] == {"call_id": "charge-2", "replayed": False}
    assert replayed["idempotency"] == {"key": "batch-1:c-001", "replayed": True}
    assert t.execution_counts == {"charge-1": 1}


@pytest.mark.asyncio
async def test_same_call_id_replay_keeps_payment_dedupe():
    t = DemoTools(payment_batch_id="batch-1")
    await t.execute("charge_card", "charge-1", {"customer_id": "c-001", "amount": "10"})
    reused = await t.execute(
        "charge_card", "charge-2", {"customer_id": "c-001", "amount": "10"}
    )
    replayed = await t.execute(
        "charge_card", "charge-2", {"customer_id": "c-001", "amount": "10"}
    )

    assert reused["execution"] == {"call_id": "charge-2", "replayed": False}
    assert reused["idempotency"] == {"key": "batch-1:c-001", "replayed": True}
    assert replayed["execution"] == {"call_id": "charge-2", "replayed": True}
    assert replayed["idempotency"] == {"key": "batch-1:c-001", "replayed": True}
    assert t.execution_counts == {"charge-1": 1}


@pytest.mark.asyncio
async def test_payment_ledger_is_shared_across_tool_instances_in_the_same_batch():
    first = DemoTools(payment_batch_id="parallel")
    second = DemoTools(payment_batch_id="parallel")
    await first.execute(
        "charge_card", "charge-1", {"customer_id": "c-001", "amount": "10"}
    )
    reused = await second.execute(
        "charge_card", "charge-2", {"customer_id": "c-001", "amount": "10"}
    )

    assert reused["idempotency"] == {"key": "parallel:c-001", "replayed": True}
    assert reused["execution"] == {"call_id": "charge-2", "replayed": False}
    assert first.execution_counts == {"charge-1": 1}
    assert second.execution_counts == {}


@pytest.mark.asyncio
async def test_payment_ledger_does_not_leak_across_batches():
    parallel = DemoTools(payment_batch_id="parallel")
    charge = DemoTools(payment_batch_id="charge")
    await parallel.execute(
        "charge_card", "charge-1", {"customer_id": "c-001", "amount": "10"}
    )
    fresh = await charge.execute(
        "charge_card", "charge-2", {"customer_id": "c-001", "amount": "49"}
    )

    assert fresh["idempotency"] == {"key": "charge:c-001", "replayed": False}
    assert charge.execution_counts == {"charge-2": 1}


@pytest.mark.asyncio
async def test_reset_payment_ledgers_clears_shared_records():
    first = DemoTools(payment_batch_id="parallel")
    await first.execute(
        "charge_card", "charge-1", {"customer_id": "c-001", "amount": "10"}
    )
    reset_payment_ledgers()
    second = DemoTools(payment_batch_id="parallel")
    fresh = await second.execute(
        "charge_card", "charge-2", {"customer_id": "c-001", "amount": "10"}
    )

    assert fresh["idempotency"]["replayed"] is False
    assert second.execution_counts == {"charge-2": 1}


@pytest.mark.asyncio
async def test_charge_card_rejects_a_changed_payment_record():
    t = DemoTools(payment_batch_id="batch-1")
    await t.execute("charge_card", "charge-1", {"customer_id": "c-001", "amount": "10"})
    conflict = await t.execute(
        "charge_card", "charge-2", {"customer_id": "c-001", "amount": "99"}
    )

    assert conflict["type"] == "error"
    assert conflict["code"] == "payment_record_conflict"
    assert conflict["idempotency"]["key"] == "batch-1:c-001"
    assert t.execution_counts == {"charge-1": 1}


@pytest.mark.asyncio
async def test_non_payment_tools_execute_again_with_a_new_call_id():
    t = DemoTools(payment_batch_id="batch-1")
    await t.execute("send_email", "send-1", {"to": "first@example.com"})
    r2 = await t.execute("send_email", "send-2", {"to": "second@example.com"})

    assert r2["execution_count"] == 1
    assert json.loads(r2["text"])["to"] == "second@example.com"
    assert t.execution_counts == {"send-1": 1, "send-2": 1}


@pytest.mark.asyncio
async def test_list_defines_four_tools():
    assert {d["name"] for d in DemoTools().list()} == {
        "remember_note",
        "read_customer",
        "charge_card",
        "send_email",
    }


@pytest.mark.asyncio
async def test_reset_payment_ledgers_reaches_a_live_instance():
    """__init__ used to cache the ledger dict, so a reset left an existing instance
    writing to an orphaned copy while a new one saw an empty ledger."""
    tools = DemoTools(payment_batch_id="parallel")
    await tools.execute("charge_card", "call-1", {"customer_id": "c-001", "amount": "10"})
    reset_payment_ledgers()
    assert tools._payment_records == {}

    fresh = DemoTools(payment_batch_id="parallel")
    await tools.execute("charge_card", "call-2", {"customer_id": "c-002", "amount": "10"})
    assert fresh._payment_records is tools._payment_records


@pytest.mark.asyncio
async def test_the_same_charge_written_differently_replays_instead_of_conflicting():
    """A rerun rendering 49 as "49.00" is the same charge. Comparing raw strings locked
    the record into a conflict that no rerun could clear."""
    tools = DemoTools(payment_batch_id="batch-1")
    await tools.execute("charge_card", "call-1", {"customer_id": "c-001", "amount": "49"})
    same = await tools.execute("charge_card", "call-2", {"customer_id": "c-001", "amount": "49.00"})
    assert same["idempotency"] == {"key": "batch-1:c-001", "replayed": True}

    changed = await tools.execute("charge_card", "call-3", {"customer_id": "c-001", "amount": "51"})
    assert changed["code"] == "payment_record_conflict"
