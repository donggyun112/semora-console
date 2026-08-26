"""Why a toggled-on unit stayed dormant in a given scenario.

A unit that is on but never fires reads as broken. The policy strip shows this reason
instead, so the viewer understands the unit was evaluated and simply had no trigger here.
"""

from __future__ import annotations

# (unit, scenario_id) -> reason. A per-unit default covers the rest.
_REASONS: dict[str, dict[str, str]] = {
    "dlp_block": {
        "customer": "외부 발송 본문에 기밀이 없음 (앞선 유닛이 가렸거나 모델이 안 담음)",
        "_": "이 시나리오엔 외부 발송 호출이 없음",
    },
    "rate_cap": {
        "_": "부작용이 예산(2) 이내",
    },
    "pii_mask": {
        "_": "툴 결과에 PII가 없음",
    },
    "context_firewall": {
        "_": "툴 결과에 기밀이 없음",
    },
    "log_gate": {
        "note": "에이전트가 이미 remember_note로 기록함 — veto 불필요",
        "_": "이미 기록됨 — veto 불필요",
    },
    "approval": {
        "_": "이 시나리오엔 부작용 호출이 없음",
    },
}


def dormant_reason(unit: str, scenario_id: str) -> str:
    """Return the reason a unit stayed dormant, specific-first then per-unit default."""
    table = _REASONS.get(unit, {})
    return table.get(scenario_id) or table.get("_") or "이 시나리오에서 트리거 없음"
