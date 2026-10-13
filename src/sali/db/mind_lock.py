"""The datastore's own assertion of "one mind" — a PostgreSQL session advisory lock.

The file lock in :mod:`sali.core.mind` is the primary guarantee, and it is enough for the case that
actually happens: several Sali processes started by the same user on this machine. This module
covers the case it cannot see.

A file lock is only shared by processes that resolve the *same path*. Two processes that disagree
about the path — a different uid (`sudo sali daemon`), a different home, a container mount, a
future second checkout — would each take "their" lock and each believe they were alone. What they
would still share is the thing that actually defines a Sali: **one `sali` database**, one memory,
one knowledge graph, one event log. So the datastore gets a vote.

``pg_try_advisory_lock`` on a dedicated connection has exactly the properties needed:

* **session-scoped** — held for as long as the connection lives, i.e. the life of the process;
* **self-releasing** — PostgreSQL drops it when the backend goes away, so a killed mind leaves no
  stale lock, exactly like flock and unlike the timeout-based ``execution_lease``;
* **observable** — ``pg_locks`` shows who holds it, so "who is the living Sali?" is answerable from
  a psql prompt with no cooperation from the process.

It needs its own connection, held open and never returned to the pool: an advisory lock taken on a
pooled connection would be released the moment that connection was recycled to another caller.

This is a *second* gate, not a replacement. The file lock works with the database down — which is
precisely when a confused operator is most likely to start a second Sali — so it stays first.
"""

from __future__ import annotations

import contextlib
from typing import Any

import asyncpg

from sali.config.settings import Settings
from sali.db.pool import init_connection

__all__ = ["MIND_ADVISORY_KEY", "DbMindLock", "current_db_mind"]

# A fixed, arbitrary 64-bit key ("5A11" = SALI). Constant across versions: changing it would let an
# old and a new Sali both "hold the lock" and coexist, which is the failure this exists to prevent.
# Its halves stay inside int4 so the pg_locks join below can reassemble it.
MIND_ADVISORY_KEY = 0x5A11_0000_0001_0001


class DbMindLock:
    """Machine-and-datastore-wide ownership of the one Sali, asserted inside PostgreSQL."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._conn: Any = None

    @property
    def held(self) -> bool:
        return self._conn is not None

    async def acquire(self) -> bool:
        """Take the advisory lock on a dedicated connection. False if another Sali holds it.

        A database that cannot be reached returns True: the file lock is the primary guarantee and
        Sali must still start (and say so) when Postgres is down, rather than refusing to exist.
        """
        if self._conn is not None:
            return True
        try:
            conn = await asyncpg.connect(
                host=self._settings.db.host, port=self._settings.db.port,
                user=self._settings.db.user, database=self._settings.db.name,
                server_settings={"search_path": "sali, public",
                                 "application_name": "sali-mind"})
            await init_connection(conn)
        except Exception:  # noqa: BLE001 - DB down: defer to the file lock, never block startup
            return True
        try:
            got = await conn.fetchval("SELECT pg_try_advisory_lock($1)", MIND_ADVISORY_KEY)
        except Exception:  # noqa: BLE001
            await _close(conn)
            return True
        if not got:
            await _close(conn)
            return False
        self._conn = conn
        return True

    async def release(self) -> None:
        """Release and close. Idempotent; a crash releases it for us when the backend dies."""
        conn, self._conn = self._conn, None
        if conn is None:
            return
        with contextlib.suppress(Exception):
            await conn.fetchval("SELECT pg_advisory_unlock($1)", MIND_ADVISORY_KEY)
        await _close(conn)


async def current_db_mind(pool: Any) -> dict[str, Any] | None:
    """Who holds the datastore's mind lock right now, straight out of ``pg_locks``.

    Truth from the database itself, not from any process's self-report — so `sali status` can show
    a living Sali it has no other way to reach.
    """
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT a.pid, a.application_name, a.client_addr, a.backend_start, a.state "
                "FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                "WHERE l.locktype = 'advisory' AND l.granted "
                "  AND ((l.classid::bigint << 32) | l.objid::bigint) = $1 "
                "LIMIT 1", MIND_ADVISORY_KEY)
    except Exception:  # noqa: BLE001 - diagnostics must never raise
        return None
    return dict(row) if row is not None else None


async def _close(conn: Any) -> None:
    with contextlib.suppress(Exception):
        await conn.close()
