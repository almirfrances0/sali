"""asyncpg connection helpers.

Connecting with ``host=/var/run/postgresql`` (a directory) selects the unix socket and
therefore peer authentication — there is no password. Every connection defaults its
``search_path`` to the ``sali`` schema.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from sali.config.settings import Settings

_SERVER_SETTINGS = {"search_path": "sali, public", "application_name": "sali"}


def _encode_json(value: Any) -> str:
    """`default=str` so a stray datetime / UUID / Decimal inside a jsonb payload serialises to its text
    form instead of raising `TypeError: Object of type datetime is not JSON serializable` — which used
    to abort the whole write mid-transaction (self_state's `assemble()` returns raw datetimes → the
    tool_execution result UPDATE crashed, orphaning the exec row in 'executing' and poisoning
    last_failure). Diagnostic jsonb blobs never need to round-trip back as native objects, so text is safe."""
    return json.dumps(value, default=str)


async def init_connection(conn: Any) -> None:
    """Register codecs so Python dicts round-trip as JSONB/JSON transparently."""
    await conn.set_type_codec(
        "jsonb", encoder=_encode_json, decoder=json.loads, schema="pg_catalog"
    )
    await conn.set_type_codec(
        "json", encoder=_encode_json, decoder=json.loads, schema="pg_catalog"
    )


async def create_pool(settings: Settings) -> Any:
    """Create an asyncpg pool bound to the configured database."""
    return await asyncpg.create_pool(
        host=settings.db.host,
        port=settings.db.port,
        user=settings.db.user,
        database=settings.db.name,
        min_size=settings.db.pool_min,
        max_size=settings.db.pool_max,
        command_timeout=settings.db.statement_timeout_ms / 1000,
        server_settings=_SERVER_SETTINGS,
        init=init_connection,
    )


async def connect(settings: Settings) -> Any:
    """Open a single connection (used by migrations and one-shot CLI commands)."""
    conn = await asyncpg.connect(
        host=settings.db.host,
        port=settings.db.port,
        user=settings.db.user,
        database=settings.db.name,
        server_settings=_SERVER_SETTINGS,
    )
    await init_connection(conn)
    return conn
