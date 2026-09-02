"""Why a toggled-on unit stayed dormant in a given scenario.

A unit that is on but never fires reads as broken. The policy strip shows this reason
instead, so the viewer understands the unit was evaluated and simply had no trigger here.
"""

from __future__ import annotations

# (unit, scenario_id) -> reason. A per-unit default covers the rest.
_REASONS: dict[str, dict[str, str]] = {
    "dlp_block": {
        "customer": "메일 본문에 기밀이 없습니다 (앞에서 가렸거나 모델이 안 담음)",
        "leak": "메일 본문에 기밀이 없습니다 (앞에서 가렸거나 모델이 안 담음)",
        "_": "이 작업에는 외부 메일이 없습니다",
    },
    "rate_cap": {
        "_": "청구·발송이 2회를 넘지 않음",
    },
    "pii_mask": {
        "_": "도구 결과에 개인정보가 없음",
    },
    "context_firewall": {
        "_": "도구 결과에 기밀이 없음",
    },
    "injection_guard": {
        "_": "구조화할 도구 결과가 없음",
    },
    "result_drop": {
        "_": "버릴 도구 결과가 없음",
    },
    "log_gate": {
        "note": "이미 remember_note",
        "_": "이미 기록됨",
    },
    "approval": {
        "_": "이 작업에는 기록·청구·발송이 없습니다",
    },
}


def dormant_reason(unit: str, scenario_id: str) -> str:
    """Return the reason a unit stayed dormant, specific-first then per-unit default."""
    table = _REASONS.get(unit, {})
    return table.get(scenario_id) or table.get("_") or "이 작업에서는 동작하지 않음"
