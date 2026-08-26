"""Step ledger selection: in-memory for local, Postgres for a durable deploy.

The suspend/resume-exactly-once claim survives a process restart only on a persistent
ledger, so a live deployment that wants the durable proof must set DATABASE_URL.

``FaultInjectingSteps`` wraps whichever ledger is in use so crash scenarios can kill
the worker at one of two seams: after ``finish_effect`` (no approval unit) or at the
first ``pre_tool_use`` (approval unit on — before the park).
"""

from __future__ import annotations

import os
from typing import Any

from nexora import Continue, MemorySteps


class SimulatedWorkerCrash(RuntimeError):
    """The worker vanished; the ledger is left as the last committed write."""

    def __init__(self, run_id: str, step: str) -> None:
        super().__init__(f"워커 장애 {step}")
        self.run_id = run_id
        self.step = step


class FaultInjectingSteps:
    """Delegate every ledger call; optionally crash after the next tool commit."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._armed: set[str] = set()
        self._gate: set[str] = set()

    def arm(self, run_id: str, *, at: str = "commit") -> None:
        self._armed.add(run_id)
        if at == "gate":
            self._gate.add(run_id)
        else:
            self._gate.discard(run_id)

    def disarm(self, run_id: str) -> None:
        self._armed.discard(run_id)
        self._gate.discard(run_id)

    def consume_gate(self, run_id: str) -> bool:
        """True once: crash at ``pre_tool_use`` before any park is written."""
        if run_id not in self._gate or run_id not in self._armed:
            return False
        self._armed.discard(run_id)
        self._gate.discard(run_id)
        return True

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
        child._gate = self._gate
        return child

    async def finish_effect(self, run_id: str, key: str, value: Any, token: int = 0) -> None:
        await self._inner.finish_effect(run_id, key, value, token)
        if (
            run_id in self._armed
            and run_id not in self._gate
            and not str(key).startswith(("agent:", "signal:", "suspend:"))
        ):
            self._armed.discard(run_id)
            raise SimulatedWorkerCrash(run_id, str(key))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def crash_before_approval(run_id: str, store: FaultInjectingSteps):
    """``pre_tool_use`` stage: die after the tool_call event, before Suspend parks."""

    async def stage(_ctx: Any, call: Any) -> Any:
        if store.consume_gate(run_id):
            raise SimulatedWorkerCrash(run_id, str(call.get("id") or call.get("name") or ""))
        return Continue()

    return stage


async def make_store() -> tuple[Any, Any]:
    """Return ``(step_store, closer)``. ``closer`` is awaited on shutdown (may be None)."""
    url = os.getenv("DATABASE_URL")
    if not url:
        return FaultInjectingSteps(MemorySteps()), None

    from nexora_store_pg import SCHEMA, PostgresSteps
    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(url, open=False)
    await pool.open()
    async with pool.connection() as conn:
        await conn.execute(SCHEMA)
    return FaultInjectingSteps(PostgresSteps(pool)), pool.close
