"""Control-plane units you compose at runtime — the substance of this demo.

nexora's control plane is seven lifecycle hooks, and every hook composes the same way:
a variadic wrapper (Permissions, Journal, FinishPolicy, …) folds many stages into one.
A unit here declares which hook it attaches to and brings one function with that hook's
signature. ``compose_controls`` groups the selected units by hook and wraps each group
with its composer, then assembles a single ``ControlPlane``.

Seam discipline (from examples/04_control_plane.py — the framework's own lesson):
policy lands at a specific seam and reaches a specific destination. We never claim a
unit reaches a destination its seam cannot touch. In particular ``pii_mask`` runs at
``after_tool_call`` and rewrites the result the MODEL and the UI see — it does NOT reach
the durable ledger copy (recorded inside the durable step, before any hook). Masking the
ledger is a different seam (a Tools wrapper); we do not pretend otherwise.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import HumanMessage
from nexora import Continue, ControlPlane, Deny, Halt, Proceed, Suspend
from nexora.contracts import ToolCall
from nexora.controls import (
    Ctx,
    FinishPolicy,
    Ingress,
    Journal,
    Permissions,
    Steering,
    Suspending,
    ToolDecision,
)

# Any tool that produces an effect (writes or leaves the system). read_customer is a pure
# read and is never counted as an effect.
EFFECTS = {"remember_note", "charge_card", "send_email"}
# The subset that cannot be undone once it runs.
IRREVERSIBLE = {"charge_card", "send_email"}
# Effects that carry data out of the org.
OUTBOUND = {"send_email"}
# Effects allowed per run before rate_cap starts denying.
BUDGET = 2

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


def _requested(ctx: Ctx, name: str) -> bool:
    """True if ``name`` was requested anywhere in this run (this round's siblings included).

    ``ctx.calls_made`` is the framework's per-run call log — the correct signal for a
    policy, because in a batched turn a sibling's result is not yet in ``ctx.messages``
    (the model has not seen it either), but the request for it is already here.
    """
    return any(c.get("name") == name for c in ctx.calls_made)


def _effect_count(ctx: Ctx) -> int:
    """Effect calls requested so far this run, including the one being decided."""
    return sum(1 for c in ctx.calls_made if c.get("name") in EFFECTS)


# ── unit implementations, one per control point ────────────────────────────────

async def approval(ctx: Ctx, call: ToolCall) -> ToolDecision:
    """pre_tool_use — suspend any effect-producing call for human sign-off; a read passes.

    Suspend halts the WHOLE loop and persists a continuation, resumable later. This is the
    verdict a middleware chain cannot express.
    """
    if call["name"] in EFFECTS:
        irreversible = call["name"] in IRREVERSIBLE
        return Suspend(
            {
                "type": "suspend",
                "pending_id": call["id"],
                "reason": (
                    f"{call['name']} is irreversible — operator sign-off required"
                    if irreversible
                    else f"{call['name']} writes an effect — operator sign-off required"
                ),
                "unit": "approval",
                "source": "pre_tool_use",
            }
        )
    return Continue()


async def dlp_block(ctx: Ctx, call: ToolCall) -> ToolDecision:
    """pre_tool_use — deny an outbound send once customer data was read this run.

    The OUTBOUND boundary (data leaving the org), honestly a block — not masking. What the
    model already saw is a separate concern owned by pii_mask (the ingest boundary).
    """
    if call["name"] in OUTBOUND and _requested(ctx, "read_customer"):
        return Deny(
            {
                "type": "error",
                "message": "dlp_block: blocked — customer data was read; outbound send refused",
                "unit": "dlp_block",
            }
        )
    return Continue()


async def rate_cap(ctx: Ctx, call: ToolCall) -> ToolDecision:
    """pre_tool_use — deny effects once this run has requested more than BUDGET of them."""
    if _effect_count(ctx) > BUDGET:
        return Deny(
            {
                "type": "error",
                "message": f"rate_cap: blocked — over {BUDGET} effects this run",
                "unit": "rate_cap",
            }
        )
    return Continue()


def _mask(text: str) -> str:
    """Redact emails and SSNs, leaving a legible stub."""
    text = _EMAIL.sub(lambda m: m.group(0)[0] + "***@***", text)
    text = _SSN.sub("***-**-****", text)
    return text


async def pii_mask(ctx: Ctx, call: ToolCall, result: dict[str, Any]) -> None:
    """after_tool_call — mask PII in a tool result IN PLACE (the ingest boundary).

    The result dict is the same object that becomes the model's tool message and the UI
    frame, so redacting it here keeps raw PII out of the model's context and the stream.
    It does NOT reach the durable ledger copy (recorded inside the durable step, before any
    hook) — masking that is a Tools-wrapper seam, out of this unit's scope by design.
    """
    if isinstance(result, dict) and isinstance(result.get("text"), str):
        masked = _mask(result["text"])
        if masked != result["text"]:
            result["text"] = masked
            result["redacted_by"] = "pii_mask"


async def log_gate(ctx: Ctx, reason: Any) -> Any:
    """before_finish — refuse to finish until the run logs its outcome, then allow.

    Vetoes completion with a steering message until ``remember_note`` has run; once it has,
    the original stop reason stands. The persistent-goal loop, expressed at one hook.
    """
    if _requested(ctx, "remember_note"):
        return Halt(reason)
    return Proceed([HumanMessage("Before finishing, call remember_note to log the outcome.")])


# ── registry ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Unit:
    """One composable control-plane unit."""

    name: str
    point: str
    composer: str
    verdict: str
    title: str
    desc: str
    fn: Callable[..., Awaitable[Any]]


UNITS: list[Unit] = [
    Unit("approval", "pre_tool_use", "Permissions", "Suspend", "승인 게이트",
         "부작용 호출(기록·청구·발송)을 사람 승인까지 루프째 정지 — 조회는 통과", approval),
    Unit("dlp_block", "pre_tool_use", "Permissions", "Deny", "유출 차단(DLP)",
         "고객 데이터를 읽은 뒤 외부 발송을 거부 — 유출 경계의 차단(마스킹 아님)", dlp_block),
    Unit("rate_cap", "pre_tool_use", "Permissions", "Deny", "부작용 예산",
         f"런당 부작용 {BUDGET}회 초과 시 거부", rate_cap),
    Unit("pii_mask", "after_tool_call", "Journal", "Rewrite", "PII 마스킹",
         "툴 결과의 이메일·SSN을 제자리 마스킹 — 모델·UI엔 원본 미유입(원장 사본은 별개 seam)", pii_mask),
    Unit("log_gate", "before_finish", "FinishPolicy", "Steer", "기록 강제",
         "remember_note로 결과를 남기기 전엔 종료를 거부하고 한 라운드 더", log_gate),
]

UNITS_BY_NAME = {u.name: u for u in UNITS}

_COMPOSERS = {
    "on_inputs": Ingress,
    "before_model": Steering,
    "pre_tool_use": Permissions,
    "after_tool_call": Journal,
    "before_finish": FinishPolicy,
    "on_suspend": Suspending,
}


def compose_controls(names: list[str]) -> ControlPlane | None:
    """Build one ControlPlane from the selected units, grouped by control point.

    Empty selection means no control plane at all — the bare loop, every tool runs.
    """
    chosen = [UNITS_BY_NAME[n] for n in names if n in UNITS_BY_NAME]
    if not chosen:
        return None
    kwargs: dict[str, Any] = {}
    for point, composer in _COMPOSERS.items():
        fns = [u.fn for u in chosen if u.point == point]
        if fns:
            kwargs[point] = composer(*fns)
    return ControlPlane(**kwargs) if kwargs else None


if __name__ == "__main__":
    import asyncio

    def _call(name: str, **args: Any) -> ToolCall:
        return {"id": "c1", "name": name, "args": args, "type": "tool_call"}

    def _ctx(*names: str) -> Ctx:
        return Ctx(turn=0, calls_made=[{"name": n, "input": {}} for n in names])

    async def _demo() -> None:
        # Permissions precedence: approval + dlp_block on an outbound send after a read → Deny wins.
        plane = compose_controls(["approval", "dlp_block"])
        assert isinstance(await plane.pre_tool_use(_ctx("read_customer", "send_email"), _call("send_email")), Deny)
        # a bare effect (no read) → Suspend
        assert isinstance(await plane.pre_tool_use(_ctx("charge_card"), _call("charge_card")), Suspend)
        # a pure read → Continue
        assert isinstance(await plane.pre_tool_use(_ctx("read_customer"), _call("read_customer")), Continue)
        # approval holds a note write too (every effect)
        assert isinstance(await compose_controls(["approval"]).pre_tool_use(_ctx("remember_note"), _call("remember_note")), Suspend)

        # Journal: pii_mask rewrites a result in place
        plane = compose_controls(["pii_mask"])
        res = {"type": "text", "text": "email=jane@doe.io ssn=123-45-6789"}
        await plane.after_tool_call(_ctx(), _call("read_customer"), res)
        assert "jane@doe.io" not in res["text"] and "123-45-6789" not in res["text"], res
        assert res["redacted_by"] == "pii_mask"

        # FinishPolicy: log_gate vetoes until the record exists, then allows
        plane = compose_controls(["log_gate"])
        assert isinstance((await plane.before_finish(_ctx("charge_card"), "completed")), Proceed)
        assert isinstance((await plane.before_finish(_ctx("remember_note"), "completed")), Halt)

        # rate_cap: deny only past the budget
        plane = compose_controls(["rate_cap"])
        assert isinstance(await plane.pre_tool_use(_ctx("charge_card"), _call("charge_card")), Continue)
        assert isinstance(await plane.pre_tool_use(_ctx("charge_card", "charge_card", "charge_card"), _call("charge_card")), Deny)

        # multi-hook selection builds one plane; empty selection is the bare loop
        assert compose_controls(["approval", "pii_mask", "log_gate"]) is not None
        assert compose_controls([]) is None
        print("units self-check ok")

    asyncio.run(_demo())
