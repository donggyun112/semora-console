"""Durable exactly-once across a simulated restart.

The claim: approval suspends a call and persists a continuation to the step ledger; a
FRESH runtime over the same (Postgres) store resumes it and runs the effect exactly once.
Skipped unless DATABASE_URL and an OpenRouter key are configured — the in-process
exactly-once is already proven live by scripts/acceptance.py (case 1, exec ×1).
"""

from __future__ import annotations

import os

import pytest

_HAS_DB = bool(os.getenv("DATABASE_URL"))
_HAS_KEY = bool(os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENROUTER_KEY") or os.getenv("OPEN_ROTURE"))

pytestmark = pytest.mark.skipif(
    not (_HAS_DB and _HAS_KEY), reason="needs DATABASE_URL (Postgres) and an OpenRouter key"
)


@pytest.mark.asyncio
async def test_suspend_survives_restart_and_runs_once():
    from nexora import AgentRuntime
    from nexora.orchestrator import AgentSuspended

    from console.provider import openrouter_model
    from console.scenarios import SYSTEM_PROMPT
    from console.store import make_store
    from console.tools import DemoTools
    from console.units import compose_controls

    store, closer = await make_store()
    try:
        run_id = "durable-test-1"
        tools = DemoTools()
        controls = compose_controls(["approval"])

        # First runtime: run until the irreversible charge suspends.
        with pytest.raises(AgentSuspended) as parked:
            await AgentRuntime(store=store).run(
                run_id, openrouter_model(), tools, "charge_card 도구로 c-001에게 49 달러를 청구해.",
                controls=controls, system_prompt=SYSTEM_PROMPT,
            )
        pending_id = parked.value.pending_id

        # Fresh runtime over the SAME store = restart. Resume by id.
        outcome = await AgentRuntime(store=store).resume(
            run_id, pending_id, {"type": "text", "text": "approved by the human"},
            openrouter_model(), tools, controls=controls, system_prompt=SYSTEM_PROMPT,
        )
        assert outcome is not None
        # The charge executed exactly once across the restart.
        assert any(v == 1 for v in tools.execution_counts.values())
    finally:
        if closer is not None:
            await closer()
