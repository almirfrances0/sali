"""Unit of Work — a transaction boundary around a pooled connection.

Memory/graph/twin mutations must be transactionally safe (engineering rule 18); this is the
single place a write transaction is opened, committed, or rolled back.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any


class UnitOfWork:
    """Acquire a pooled connection and run inside one transaction.

    Commits on clean exit, rolls back on exception — the connection is always released.
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool
        self._conn: Any = None
        self._tx: Any = None

    async def __aenter__(self) -> Any:
        self._conn = await self._pool.acquire()
        self._tx = self._conn.transaction()
        await self._tx.start()
        return self._conn

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if exc_type is not None:
                await self._tx.rollback()
            else:
                await self._tx.commit()
        finally:
            await self._pool.release(self._conn)
