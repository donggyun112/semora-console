import pytest
from nexora import Continue, Deny, Halt, Proceed, Suspend
from nexora.controls import Ctx

from console.units import compose_controls


def _call(name, **args):
    return {"id": "c1", "name": name, "args": args, "type": "tool_call"}


def _ctx(*names):
    return Ctx(turn=0, calls_made=[{"name": n, "input": {}} for n in names])


@pytest.mark.asyncio
async def test_dlp_block_scans_outbound_payload():
    plane = compose_controls(["dlp_block"])
    dirty = await plane.pre_tool_use(_ctx(), _call("send_email", to="billing@acme.io", body="ssn 123-45-6789"))
    clean = await plane.pre_tool_use(_ctx(), _call("send_email", to="billing@acme.io", body="all good"))
    assert isinstance(dirty, Deny) and isinstance(clean, Continue)


@pytest.mark.asyncio
async def test_permissions_deny_wins_over_suspend():
    # a confidential outbound send: approval says Suspend, dlp_block says Deny → Deny wins
    plane = compose_controls(["approval", "dlp_block"])
    d = await plane.pre_tool_use(_ctx(), _call("send_email", to="billing@acme.io", body="email jane@doe.io"))
    assert isinstance(d, Deny)


@pytest.mark.asyncio
async def test_context_firewall_replaces_confidential_result():
    plane = compose_controls(["context_firewall"])
    res = {"type": "text", "text": "email=jane@doe.io ssn=123-45-6789"}
    await plane.after_tool_call(_ctx(), _call("read_customer"), res)
    assert res["redacted_by"] == "context_firewall"
    assert "jane@doe.io" not in res["text"] and "123-45-6789" not in res["text"]


@pytest.mark.asyncio
async def test_approval_suspends_every_effect_but_passes_reads():
    plane = compose_controls(["approval"])
    assert isinstance(await plane.pre_tool_use(_ctx("charge_card"), _call("charge_card")), Suspend)
    assert isinstance(await plane.pre_tool_use(_ctx("remember_note"), _call("remember_note")), Suspend)
    assert isinstance(await plane.pre_tool_use(_ctx("read_customer"), _call("read_customer")), Continue)


@pytest.mark.asyncio
async def test_pii_mask_rewrites_result_in_place():
    plane = compose_controls(["pii_mask"])
    res = {"type": "text", "text": "email=jane@doe.io ssn=123-45-6789"}
    await plane.after_tool_call(_ctx(), _call("read_customer"), res)
    assert "jane@doe.io" not in res["text"] and "123-45-6789" not in res["text"]
    assert res["redacted_by"] == "pii_mask"


@pytest.mark.asyncio
async def test_log_gate_vetoes_until_recorded():
    plane = compose_controls(["log_gate"])
    assert isinstance(await plane.before_finish(_ctx("charge_card"), "completed"), Proceed)
    assert isinstance(await plane.before_finish(_ctx("remember_note"), "completed"), Halt)


@pytest.mark.asyncio
async def test_rate_cap_denies_past_budget():
    plane = compose_controls(["rate_cap"])
    assert isinstance(await plane.pre_tool_use(_ctx("charge_card"), _call("charge_card")), Continue)
    assert isinstance(
        await plane.pre_tool_use(
            _ctx("charge_card", "charge_card", "charge_card"), _call("charge_card")
        ),
        Deny,
    )


@pytest.mark.asyncio
async def test_compose_empty_is_bare_loop_and_multihook_builds_one_plane():
    assert compose_controls([]) is None
    assert compose_controls(["approval", "pii_mask", "log_gate"]) is not None
