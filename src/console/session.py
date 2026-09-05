"""Leased business effects shared by every execution version of a request."""

import asyncio
import inspect
import uuid
from contextvars import ContextVar
from typing import Any

from semora_store import Contended, Indeterminate

_held: ContextVar[tuple[str, int] | None] = ContextVar(
    "console_session_lease", default=None
)


def session_step(store: Any, session_id: str, *, ttl: float = 60.0):
    """Execute each session key once, preserving uncertainty after interruption."""

    async def execute(key, body, token):
        previous = await store.read(session_id, key)
        if previous.status == "done":
            return previous.value
        if previous.status == "running" or not await store.start(
            session_id, key, token
        ):
            raise Indeterminate(session_id, key)
        # An exception cannot prove whether this business effect left the process.
        # Preserve its intent; only a recorded result authorizes a safe replay.
        value = body()
        if inspect.isawaitable(value):
            value = await value
        await store.finish_effect(session_id, key, value, token)
        return value

    async def run(key, body):
        held = _held.get()
        if held is not None and held[0] == session_id:
            return await execute(key, body, held[1])
        owner = f"console-effect:{uuid.uuid4().hex}"
        token = await store.acquire(session_id, owner, ttl)
        if not token:
            raise Contended(session_id)
        context = _held.set((session_id, token))
        parent = asyncio.current_task()

        async def renew():
            while True:
                await asyncio.sleep(ttl / 3)
                if await store.acquire(session_id, owner, ttl) != token:
                    if parent is not None:
                        parent.cancel()
                    return

        renewer = asyncio.create_task(renew())
        try:
            return await execute(key, body, token)
        finally:
            renewer.cancel()
            await asyncio.gather(renewer, return_exceptions=True)
            _held.reset(context)
            await store.release(session_id, owner)

    return run
