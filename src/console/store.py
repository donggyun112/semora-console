"""Step and transcript store selection for local or durable deployment.

Suspension, recovery, and exactly-once effects survive a process restart only when both
the step ledger and transcript are persistent, so durable deployments set DATABASE_URL.

``FaultInjectingSteps`` wraps whichever ledger is in use so crash scenarios can kill
the worker at one of three seams: after ``finish_effect`` (the effect committed), at the
first ``pre_tool_use`` (before the park), or after the tool ran and before its result was
recorded. Only the last leaves a step ``running`` with the effect already out in the
world, which is the state nobody can decide from the ledger alone.
"""

from __future__ import annotations

import os
from typing import Any

from nexora import Continue, MemorySteps
from nexora_store import MemoryTranscript


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
        self._effect: set[str] = set()

    def arm(self, run_id: str, *, at: str = "commit") -> None:
        self._armed.add(run_id)
        self._gate.discard(run_id)
        self._effect.discard(run_id)
        if at == "gate":
            self._gate.add(run_id)
        elif at == "effect":
            self._effect.add(run_id)

    def disarm(self, run_id: str) -> None:
        self._armed.discard(run_id)
        self._gate.discard(run_id)
        self._effect.discard(run_id)

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
        child._effect = self._effect
        return child

    async def finish_effect(self, run_id: str, key: str, value: Any, token: int = 0) -> None:
        if (
            run_id in self._armed
            and run_id in self._effect
            and not str(key).startswith(("agent:", "signal:", "suspend:", "after:"))
        ):
            # Before delegating, so the tool has run and its result never lands. The step
            # stays running with the effect already out — the one case the ledger cannot
            # settle, and the caller has to.
            self._armed.discard(run_id)
            self._effect.discard(run_id)
            raise SimulatedWorkerCrash(run_id, str(key))
        await self._inner.finish_effect(run_id, key, value, token)
        if (
            run_id in self._armed
            and run_id not in self._gate
            and run_id not in self._effect
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


async def make_store() -> tuple[Any, Any, Any]:
    """Return the step store, transcript store, and optional async closer."""
    url = os.getenv("DATABASE_URL")
    if not url:
        return FaultInjectingSteps(MemorySteps()), MemoryTranscript(), None

    from nexora_store_pg import (
        SCHEMA,
        TRANSCRIPT_SCHEMA,
        PostgresSteps,
        PostgresTranscript,
    )
    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(url, open=False)
    await pool.open()
    async with pool.connection() as conn:
        await conn.execute(SCHEMA)
        await conn.execute(TRANSCRIPT_SCHEMA)
    return FaultInjectingSteps(PostgresSteps(pool)), PostgresTranscript(pool), pool.close
