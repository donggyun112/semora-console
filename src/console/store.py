"""Step ledger selection: in-memory for local, Postgres for a durable deploy.

The suspend/resume-exactly-once claim survives a process restart only on a persistent
ledger, so a live deployment that wants the durable proof must set DATABASE_URL.
"""

from __future__ import annotations

import os
from typing import Any

from nexora import MemorySteps


async def make_store() -> tuple[Any, Any]:
    """Return ``(step_store, closer)``. ``closer`` is awaited on shutdown (may be None)."""
    url = os.getenv("DATABASE_URL")
    if not url:
        return MemorySteps(), None

    from nexora_store_pg import SCHEMA, PostgresSteps
    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(url, open=False)
    await pool.open()
    async with pool.connection() as conn:
        await conn.execute(SCHEMA)
    return PostgresSteps(pool), pool.close
