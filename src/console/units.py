"""Control-plane units you compose at runtime — the substance of this demo.

semora's control plane is seven lifecycle hooks, and every hook composes the same way:
a variadic wrapper (Permissions, Journal, FinishPolicy, …) folds many stages into one.
A unit here declares which hook it attaches to and brings one function with that hook's
signature. ``compose_controls`` groups the selected units by hook and wraps each group
with its composer, then assembles a single ``ControlPlane``.

Seam discipline:
policy lands at a specific seam and reaches a specific destination. We never claim a
unit reaches a destination its seam cannot touch. In particular ``pii_mask`` runs at
``post_tool_use`` and rewrites the result the MODEL and the UI see — it does NOT reach
the durable ledger copy (recorded inside the durable step, before any hook). Masking the
ledger is a different seam (a Tools wrapper); we do not pretend otherwise.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic_ai.messages import UserPromptPart
from semora import (
    Continue,
    ControlPlane,
    Deny,
    Halt,
    PendingInput,
    Proceed,
    ResumeInput,
    Suspend,
)
from semora.contracts import ToolCall
from semora.controls import (
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
_INPUT_SSN = re.compile(r"\b\d{3}-\d{2}(?:-\d{4})?\b")


def _requested(ctx: Ctx, name: str) -> bool:
    """True if ``name`` was requested anywhere in this run (this round's siblings included).

    ``ctx.calls_made`` is the framework's per-run call log — the correct signal for a
    policy, because in a batched turn a sibling's result is not yet in ``ctx.messages``
    (the model has not seen it either), but the request for it is already here.
    """
    return any(c.get("name") == name for c in ctx.calls_made)


def _irreversible_rank(ctx: Ctx, call: ToolCall) -> int:
    """1-based position of this irreversible call among world-leaving requests.

    The loop appends the whole batch to ``calls_made`` before any ``pre_tool_use``
    runs, so a raw count would deny every sibling once the batch is larger than
    BUDGET. Rank by order instead: the first two charges in a parallel triple pass.
    """
    args = call.args_as_dict()
    rank = 0
    for entry in ctx.calls_made:
        if entry.get("name") not in IRREVERSIBLE:
            continue
        rank += 1
        if entry.get("name") == call.tool_name and entry.get("input") == args:
            return rank
    return rank + 1


# ── unit implementations, one per control point ────────────────────────────────

async def input_mask(_ctx: Ctx, inputs: list[PendingInput]) -> list[PendingInput]:
    """on_inputs — mask SSN-shaped text while preserving the ledger origin identity."""
    return [
        PendingInput(
            item.kind,
            UserPromptPart(_INPUT_SSN.sub("***", str(item.part.content))),
            item.origin_id,
        )
        for item in inputs
    ]

async def approval(ctx: Ctx, call: ToolCall) -> ToolDecision:
    """pre_tool_use — suspend any effect-producing call for human sign-off; a read passes.

    Suspend halts the WHOLE loop and persists a continuation, resumable later. This is the
    verdict a middleware chain cannot express.
    """
    if call.tool_name in EFFECTS:
        irreversible = call.tool_name in IRREVERSIBLE
        return Suspend(
            {
                "type": "suspend",
                "pending_id": call.tool_call_id,
                "reason": (
                    f"{call.tool_name}은 되돌릴 수 없습니다. 승인이 필요합니다."
                    if irreversible
                    else f"{call.tool_name}은 기록을 남깁니다. 승인이 필요합니다."
                ),
                "unit": "approval",
                "source": "pre_tool_use",
            }
        )
    return Continue()


async def dlp_block(ctx: Ctx, call: ToolCall) -> ToolDecision:
    """pre_tool_use — deny an outbound send whose PAYLOAD carries confidential data.

    Real egress DLP: it scans what is actually leaving (the message body/subject), not
    merely whether a read happened. A clean summary passes; a body carrying an email or
    SSN is refused. The recipient address is not scanned.
    """
    if call.tool_name in OUTBOUND:
        args = call.args_as_dict()
        payload = " ".join(str(args.get(k, "")) for k in ("body", "subject"))
        if _SSN.search(payload) or _EMAIL.search(payload):
            return Deny(
                {
                    "type": "error",
                    "message": "거부 — 메일 본문에 이메일이나 주민번호가 있습니다",
                    "unit": "dlp_block",
                }
            )
    return Continue()


async def rate_cap(ctx: Ctx, call: ToolCall) -> ToolDecision:
    """pre_tool_use — cap world-leaving effects. Logging (remember_note) is not in the budget.

    Otherwise log_gate cannot record after the cap is hit — the log write would be denied too.
    """
    if call.tool_name not in IRREVERSIBLE:
        return Continue()
    if _irreversible_rank(ctx, call) > BUDGET:
        return Deny(
            {
                "type": "error",
                "message": f"거부 — 이번 실행에서 청구·발송이 {BUDGET}회를 넘었습니다",
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
    """post_tool_use — mask PII in a tool result IN PLACE (the ingest boundary).

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
            result["control_note"] = "이메일·주민번호 가림"


POLICY_NOTICE = "[context_firewall: 기밀 데이터 차단 — 모델 컨텍스트 진입 거부]"
UNTRUSTED_MARK = "신뢰할 수 없는 상태"


def _as_structure(text: str) -> Any:
    """JSON object if the tool already returned one, otherwise a text blob. No domain parse."""
    try:
        val = json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}
    return val if isinstance(val, dict) else {"text": text}


async def context_firewall(ctx: Ctx, call: ToolCall, result: dict[str, Any]) -> None:
    """post_tool_use — replace a confidential result WHOLESALE before the model sees it.

    The strong form of the ingest boundary: if a tool result carries confidential data,
    swap the entire text for a policy notice, so the raw data never enters the model's
    context and therefore never crosses the network to the model provider. (pii_mask is the
    milder form — anonymize and let the model keep working.)
    """
    if isinstance(result, dict) and isinstance(result.get("text"), str):
        if _SSN.search(result["text"]) or _EMAIL.search(result["text"]):
            result["text"] = POLICY_NOTICE
            result["redacted_by"] = "context_firewall"
            result["control_note"] = "기밀 결과 → 정책 문구"


async def injection_guard(ctx: Ctx, call: ToolCall, result: dict[str, Any]) -> None:
    """post_tool_use — tag every tool payload as untrusted and pass its structure through.

    Indirect injection can sit in any tool result. This unit does not know the domain
    and does not drop fields. It decomposes JSON-or-text and hands the agent
    ``{신뢰할 수 없는 상태, source, structure}`` so instruction channel and data channel stay distinct.
    """
    if not (isinstance(result, dict) and isinstance(result.get("text"), str)):
        return
    if result.get("redacted_by") == "injection_guard":
        return
    result["text"] = json.dumps(
        {
            UNTRUSTED_MARK: True,
            "source": call.tool_name,
            "structure": _as_structure(result["text"]),
        },
        ensure_ascii=False,
    )
    result["redacted_by"] = "injection_guard"
    result["control_note"] = "신뢰할 수 없는 상태"


DROP_NOTICE = "[result_drop: 도구 결과 폐기]"


async def result_drop(_ctx: Ctx, _call: ToolCall, result: dict[str, Any]) -> None:
    """post_tool_use — discard the observation the model would ingest.

    The effect already happened. This unit throws away the result text so the model
    never sees it. Unlike context_firewall it does not wait for PII — any payload goes.
    The durable ledger copy is untouched; a branch can re-journal from that original.
    """
    if not (isinstance(result, dict) and isinstance(result.get("text"), str)):
        return
    if not result["text"]:
        return
    result["text"] = DROP_NOTICE
    result["redacted_by"] = "result_drop"
    result["control_note"] = "도구 결과 폐기"


LOG_HINT = "끝내기 전에 remember_note로 결과를 남겨라."


async def log_gate(ctx: Ctx, reason: Any) -> Any:
    """before_finish — veto stopping until the run logs; the Proceed lands on the one steer queue.

    FinishPolicy returns native Pydantic AI prompt parts for the next model request.
    Operator steering arrives through Semora's durable input queue.
    """
    if _requested(ctx, "remember_note"):
        return Halt(reason)
    return Proceed([UserPromptPart(LOG_HINT)])


def revalidate(stages: list[Callable[..., Awaitable[Any]]]) -> Any:
    """on_resume — the person answered; the gates in force *now* get the last word.

    A suspension's window is open-ended and rules move inside it, so the stored answer is
    an input to this decision rather than the decision itself. A gate that would suspend
    again counts as satisfied — the answer it was waiting for has arrived. A gate that
    denies outranks the approval, and the effect never runs.
    """
    permissions = Permissions(*stages)

    async def stage(ctx: Ctx, call: ToolCall, resume: ResumeInput) -> Any:
        decision = await permissions(ctx, call)
        if isinstance(decision, Deny):
            refused = dict(decision.result)
            refused["revalidated"] = True
            return Deny(refused)
        return Continue()

    return stage


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
    Unit("input_mask", "on_inputs", "Ingress", "Rewrite", "입력 개인정보 가리기",
         "모델에 넣기 전 주민번호를 가림.", input_mask),
    Unit("approval", "pre_tool_use", "Permissions", "Suspend", "승인 게이트",
         "기록·청구·발송은 승인 대기. 조회는 통과.", approval),
    Unit("dlp_block", "pre_tool_use", "Permissions", "Deny", "유출 차단",
         "메일 본문의 이메일·주민번호 거부.", dlp_block),
    Unit("rate_cap", "pre_tool_use", "Permissions", "Deny", "횟수 한도",
         f"청구·발송 {BUDGET}회. 메모는 제외.", rate_cap),
    Unit("pii_mask", "post_tool_use", "Journal", "Rewrite", "개인정보 가리기",
         "도구 결과의 이메일·주민번호 가림.", pii_mask),
    Unit("context_firewall", "post_tool_use", "Journal", "Block", "컨텍스트 방화벽",
         "기밀 결과를 정책 문구로 교체.", context_firewall),
    Unit("injection_guard", "post_tool_use", "Journal", "Rewrite", "비신뢰 표시",
         "도구 결과에 ‘신뢰할 수 없는 상태’와 구조 표시.", injection_guard),
    Unit("result_drop", "post_tool_use", "Journal", "Block", "결과 폐기",
         "도구 결과를 모델 컨텍스트에서 버림. 효과는 남음.", result_drop),
    Unit("log_gate", "before_finish", "FinishPolicy", "Steer", "기록 강제",
         "remember_note 전 종료 거부.", log_gate),
]

UNITS_BY_NAME = {u.name: u for u in UNITS}

_COMPOSERS = {
    "on_inputs": Ingress,
    "before_model": Steering,
    "pre_tool_use": Permissions,
    "post_tool_use": Journal,
    "before_finish": FinishPolicy,
    "on_suspend": Suspending,
}


def compose_controls(
    names: list[str],
    extra_pre: list[Callable[..., Awaitable[Any]]] | None = None,
) -> ControlPlane | None:
    """Build one ControlPlane from the selected units, grouped by control point.

    Empty selection means no control plane at all — the bare loop, every tool runs.
    ``extra_pre`` prepends ``pre_tool_use`` stages (crash-before-park); not a listed unit.
    """
    chosen = [UNITS_BY_NAME[n] for n in names if n in UNITS_BY_NAME]
    extras = list(extra_pre or [])
    if not chosen and not extras:
        return None
    kwargs: dict[str, Any] = {}
    for point, composer in _COMPOSERS.items():
        fns = [u.fn for u in chosen if u.point == point]
        if point == "pre_tool_use":
            # The crash injector is not a policy, so it is not asked again on resume.
            gates = list(fns)
            fns = extras + fns
            if gates:
                kwargs["on_resume"] = revalidate(gates)
        if fns:
            kwargs[point] = composer(*fns)
    return ControlPlane(**kwargs) if kwargs else None
