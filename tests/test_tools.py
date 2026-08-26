import pytest

from console.tools import DemoTools


@pytest.mark.asyncio
async def test_read_customer_carries_pii_and_counts():
    t = DemoTools()
    r = await t.execute("read_customer", "id1", {"customer_id": "c-001"})
    assert "@" in r["text"] and "-" in r["text"]
    assert r["execution_count"] == 1


@pytest.mark.asyncio
async def test_execution_count_increments_per_call_id():
    t = DemoTools()
    await t.execute("charge_card", "x", {"amount": "10"})
    r2 = await t.execute("charge_card", "x", {"amount": "10"})
    assert r2["execution_count"] == 2


@pytest.mark.asyncio
async def test_list_defines_four_tools():
    assert {d["name"] for d in DemoTools().list()} == {
        "remember_note",
        "read_customer",
        "charge_card",
        "send_email",
    }
