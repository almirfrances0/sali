"""Database initialization entrypoint used by ``sali init``.

Assumes the role and databases already exist (created once by ``scripts/bootstrap_db.sh``,
which needs the privileges Sali itself deliberately does not have).
"""

from __future__ import annotations

from sali.config.settings import Settings
from sali.db.migrations.runner import apply_migrations
from sali.db.pool import connect


async def init_database(settings: Settings) -> list[str]:
    """Apply outstanding migrations to the configured database; return newly-applied versions."""
    conn = await connect(settings)
    try:
        return await apply_migrations(conn)
    finally:
        await conn.close()
