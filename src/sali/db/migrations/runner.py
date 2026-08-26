"""A tiny forward-only migration runner.

Each ``NNNN_name.sql`` file under this directory is applied at most once, inside its own
transaction, and recorded in ``sali.schema_migrations``. DDL in Postgres is transactional,
so a failed migration leaves no partial schema behind.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sali.core.errors import MigrationError

MIGRATIONS_DIR = Path(__file__).parent


async def apply_migrations(conn: Any, migrations_dir: Path | None = None) -> list[str]:
    """Apply any unapplied migrations in order; return the versions newly applied."""
    directory = migrations_dir or MIGRATIONS_DIR
    await conn.execute("CREATE SCHEMA IF NOT EXISTS sali")
    await conn.execute("SET search_path = sali, public")
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  version text PRIMARY KEY,"
        "  applied_at timestamptz NOT NULL DEFAULT now())"
    )
    applied = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}

    newly: list[str] = []
    for path in sorted(directory.glob("*.sql")):
        version = path.stem
        if version in applied:
            continue
        sql = path.read_text(encoding="utf-8")
        try:
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES ($1)", version
                )
        except Exception as exc:  # noqa: BLE001 - re-raised as a typed error
            raise MigrationError(f"migration {version} failed: {exc}") from exc
        newly.append(version)
    return newly


async def current_version(conn: Any) -> str | None:
    """The highest applied migration version, or None if the table is absent/empty."""
    exists = await conn.fetchval(
        "SELECT to_regclass('sali.schema_migrations') IS NOT NULL"
    )
    if not exists:
        return None
    row = await conn.fetchrow(
        "SELECT version FROM sali.schema_migrations ORDER BY version DESC LIMIT 1"
    )
    return row["version"] if row else None
