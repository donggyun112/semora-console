"""Step and transcript store selection for local or durable deployment.

Suspension, recovery, and exactly-once effects survive a process restart only when both
the step ledger and transcript are persistent, so durable deployments set DATABASE_URL.

``FaultInjectingSteps`` wraps whichever ledger is in use so a scenario can kill the
worker at one of three named seams:

``gate``    at the first ``pre_tool_use``, before a suspension is parked. Nothing ran.
``commit``  after a tool result is recorded. The effect happened and is known to have.
``effect``  after a charge left and before its record lands, so the step stays running
            with the money already gone — the one state the ledger cannot settle.

``effect`` watches the session that owns the charge rather than the agent run that asked
for it. Aiming it at the agent's call step used to leave the charge safely recorded in
the session while the console announced nobody could tell, which was a demo lying about
its own ledger.
"""

from __future__ import annotations

import os
from typing import Any

from semora import Continue, MemorySteps
from semora.contracts import ControlSignal
from semora_store import MemoryTranscript


class SimulatedWorkerCrash(ControlSignal):
    """The worker vanished; the ledger is left as the last committed write.

    A ``ControlSignal``, so the tool boundary lets it through. A worker dying is not the
    tool reporting failure — treated as one, the round would carry on with an error
    result and the run would never look interrupted at all.
    """

    def __init__(self, branch_id: str, step: str) -> None:
        super().__init__(f"워커 장애 {step}")
        self.branch_id = branch_id
        self.step = step


# Keys the runtime writes for its own bookkeeping. A crash on one of these would be a
# crash in the machinery rather than in an effect, which is not what any scenario means.
_BOOKKEEPING = ("agent:", "signal:", "suspend:", "after:")


class FaultInjectingSteps:
    """Delegate every ledger call, and fire one armed seam once."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._armed: dict[str, str] = {}
        """run id → the seam armed for it. One entry, one crash, then disarmed."""

    def arm(self, watched_id: str, *, at: str = "commit") -> None:
        """Arm one seam. ``effect`` watches a session id, the others an agent run."""
        self._armed[watched_id] = at

    def disarm(self, watched_id: str) -> None:
        self._armed.pop(watched_id, None)

    def _fires(self, watched_id: str, seam: str) -> bool:
        """True once, then never again for that id."""
        if self._armed.get(watched_id) != seam:
            return False
        del self._armed[watched_id]
        return True

    def consume_gate(self, branch_id: str) -> bool:
        """True once: crash at ``pre_tool_use`` before any park is written."""
        return self._fires(branch_id, "gate")

    def for_execution(self, context: Any) -> FaultInjectingSteps:
        """Keep the crash hook when the runtime scopes the ledger.

        ``MemorySteps.for_execution`` returns the inner store. Forwarding that through
        ``__getattr__`` dropped ``finish_effect`` and the crash never fired.
        """
        inner = self._inner.for_execution(context)
        if inner is self._inner:
            return self
        child = FaultInjectingSteps(inner)
        child._armed = self._armed
        return child

    async def finish_effect(self, branch_id: str, key: str, value: Any, token: int = 0) -> None:
        step = str(key)
        if step.startswith(_BOOKKEEPING):
            await self._inner.finish_effect(branch_id, key, value, token)
            return
        # Before delegating: the charge left and its record never lands.
        if step.startswith("charge:") and self._fires(branch_id, "effect"):
            raise SimulatedWorkerCrash(branch_id, step)
        await self._inner.finish_effect(branch_id, key, value, token)
        # After delegating: the effect is recorded, and recovery has nothing to decide.
        if self._fires(branch_id, "commit"):
            raise SimulatedWorkerCrash(branch_id, step)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def crash_before_approval(branch_id: str, store: FaultInjectingSteps):
    """``pre_tool_use`` stage: die after the tool_call event, before Suspend parks."""

    async def stage(_ctx: Any, call: Any) -> Any:
        if store.consume_gate(branch_id):
            raise SimulatedWorkerCrash(branch_id, call.tool_call_id)
        return Continue()

    return stage


async def make_store() -> tuple[Any, Any, Any]:
    """Return the step store, transcript store, and optional async closer."""
    url = os.getenv("DATABASE_URL")
    if not url:
        return FaultInjectingSteps(MemorySteps()), MemoryTranscript(), None

    from psycopg_pool import AsyncConnectionPool
    from semora_store_pg import (
        SCHEMA,
        TRANSCRIPT_SCHEMA,
        PostgresSteps,
        PostgresTranscript,
    )

    pool = AsyncConnectionPool(url, open=False)
    await pool.open()
    async with pool.connection() as conn:
        await conn.execute(SCHEMA)
        await conn.execute(TRANSCRIPT_SCHEMA)
    return FaultInjectingSteps(PostgresSteps(pool)), PostgresTranscript(pool), pool.close
