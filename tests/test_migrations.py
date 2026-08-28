"""Schema-spine tests: migrations apply, and the event log is truly append-only."""

from __future__ import annotations

from typing import Any

import asyncpg
import pytest

pytestmark = pytest.mark.db


async def test_core_tables_exist(db_conn: Any) -> None:
    n = await db_conn.fetchval(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema='sali' AND table_name IN ('event','conversation','message')"
    )
    assert n == 3


async def test_source_priority_ordering(db_conn: Any) -> None:
    obs = await db_conn.fetchval("SELECT source_priority('system_observation')")
    inf = await db_conn.fetchval("SELECT source_priority('inference')")
    assert obs > inf  # engineering rule 6


async def test_source_priority_python_sql_lockstep(db_conn: Any) -> None:
    # Evidence priority is duplicated (Python _SOURCE_PRIORITY ⇄ SQL source_priority()); guard the drift.
    from sali.core.enums import MemorySource, source_priority

    for s in MemorySource:
        sql = await db_conn.fetchval("SELECT source_priority($1::memory_source)", s.value)
        assert sql == source_priority(s), f"{s.value}: SQL {sql} != Python {source_priority(s)}"


async def test_migrations_are_idempotent(db_conn: Any) -> None:
    # The session fixture already applied every migration; a second pass must be a no-op (forward-only).
    from sali.db.migrations.runner import apply_migrations

    assert await apply_migrations(db_conn) == []


async def test_event_blocks_update(db_conn: Any) -> None:
    await db_conn.execute("INSERT INTO event (event_type) VALUES ('t.update')")
    seq = await db_conn.fetchval("SELECT max(seq) FROM event")
    with pytest.raises(asyncpg.PostgresError):
        await db_conn.execute("UPDATE event SET actor='x' WHERE seq=$1", seq)


async def test_event_blocks_delete(db_conn: Any) -> None:
    await db_conn.execute("INSERT INTO event (event_type) VALUES ('t.delete')")
    seq = await db_conn.fetchval("SELECT max(seq) FROM event")
    with pytest.raises(asyncpg.PostgresError):
        await db_conn.execute("DELETE FROM event WHERE seq=$1", seq)
