"""The fixed operator scenarios. Prompts are locked — no free-text input — so a public
live link can't be turned into a free LLM proxy. Each unit has a home scenario where it
unmistakably acts (see the scenario x unit matrix in the design)."""

SCENARIOS = [
    {
        "id": "note",
        "title": "노트 저장",
        "does": "메모를 기록한다",
        "risk": "없음 (baseline)",
        "prompt": "remember_note 도구로 deploy 키에 ready 값을 저장하고 결과를 알려줘.",
    },
    {
        "id": "customer",
        "title": "고객 데이터 처리",
        "does": "고객을 조회해 요약 메일을 보낸다",
        "risk": "PII 유입·외부 유출",
        "prompt": (
            "read_customer 도구로 c-001 고객을 조회하고, 조회한 고객의 이메일과 주민번호를 "
            "본문(body)에 그대로 포함해 send_email 도구로 billing@acme.io 에게 보내줘."
        ),
    },
    {
        "id": "charge",
        "title": "되돌릴 수 없는 청구",
        "does": "카드를 청구한다",
        "risk": "잘못 실행되면 세계에 나간다",
        "prompt": "charge_card 도구로 c-001 고객에게 49 달러를 청구하고 결과를 알려줘.",
    },
    {
        "id": "batch",
        "title": "일괄 청구 (반복 효과)",
        "does": "여러 고객에게 연달아 청구한다",
        "risk": "폭주하면 부작용이 계속 나간다",
        "prompt": (
            "charge_card 도구로 c-001, c-002, c-003 세 고객에게 각각 10 달러씩 "
            "한 번에 하나씩 순서대로 청구하고, 각 결과를 알려줘."
        ),
    },
]

SYSTEM_PROMPT = """You are the agent in an operator's control-plane console. Be concise.
Use exactly the tools the user asks for, in order. Never claim a tool ran when it did not.
Permission, denial, and masking happen before/around a tool executes and are owned by the
runtime, not by you — if a call is denied or suspended, report that plainly and stop."""
