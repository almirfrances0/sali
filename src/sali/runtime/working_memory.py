"""Persistent working memory — Sali's mental workspace.

This is NOT another memory layer. It is the active cognitive state that the LLM explicitly
manages: what problem is being worked on, what hypotheses exist, what decisions were made,
what assumptions underlie current reasoning.

Working memory persists across turns, context compactions, and restarts. It is the bridge
between the LLM's ephemeral reasoning and the durable datastore.

Sali is a digital person. Working memory is her stream of consciousness — not the full
inner monologue (that's the LLM's reasoning), but the structured state that helps her
pick up where she left off.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sali.obs.log import get_logger

log = get_logger("sali.working_memory")

# Categories the LLM can set
_CATEGORIES = frozenset({
    "active_problem", "hypothesis", "assumption", "decision",
    "planned_action", "observation", "question", "context", "directive",
})


class WorkingMemoryStore:
    """Manages Sali's persistent mental workspace."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def set(
        self,
        session_id: str,
        category: str,
        content: str,
        *,
        confidence: float = 0.5,
        source: str = "reasoning",
    ) -> UUID:
        """Set or update a working memory entry. Supersedes any existing entry in the same category
        for this session. Returns the new entry's ID."""
        if category not in _CATEGORIES:
            raise ValueError(f"invalid category: {category}")
        entry_id = uuid4()
        async with self._pool.acquire() as conn:
            # Supersede existing entry in same category
            existing = await conn.fetchval(
                "SELECT id FROM sali.working_memory "
                "WHERE session_id=$1 AND category=$2 AND superseded_by IS NULL "
                "AND (expires_at IS NULL OR expires_at > now()) "
                "ORDER BY updated_at DESC LIMIT 1",
                session_id, category)
            if existing:
                await conn.execute(
                    "UPDATE sali.working_memory SET superseded_by=$1, updated_at=now() WHERE id=$2",
                    entry_id, existing)
            await conn.execute(
                "INSERT INTO sali.working_memory "
                "(id, session_id, category, content, confidence, source) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                entry_id, session_id, category, content, confidence, source)
        return entry_id

    async def get(self, session_id: str, category: str) -> dict[str, Any] | None:
        """Get the current working memory entry for a category."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, category, content, confidence, source, created_at, updated_at "
                "FROM sali.working_memory "
                "WHERE session_id=$1 AND category=$2 AND superseded_by IS NULL "
                "AND (expires_at IS NULL OR expires_at > now()) "
                "ORDER BY updated_at DESC LIMIT 1",
                session_id, category)
        if row is None:
            return None
        return {
            "id": str(row["id"]), "category": row["category"],
            "content": row["content"], "confidence": float(row["confidence"]),
            "source": row["source"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }

    async def snapshot(self, session_id: str) -> list[dict[str, Any]]:
        """Get all current working memory entries for a session. This is what the LLM sees."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, category, content, confidence, source, created_at, updated_at "
                "FROM sali.working_memory "
                "WHERE session_id=$1 AND superseded_by IS NULL "
                "AND (expires_at IS NULL OR expires_at > now()) "
                "ORDER BY updated_at DESC",
                session_id)
        return [{
            "id": str(r["id"]), "category": r["category"],
            "content": r["content"], "confidence": float(r["confidence"]),
            "source": r["source"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        } for r in rows]

    async def clear(self, session_id: str, category: str) -> bool:
        """Clear all entries in a category for a session."""
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE sali.working_memory SET expires_at=now() "
                "WHERE session_id=$1 AND category=$2 AND superseded_by IS NULL "
                "AND (expires_at IS NULL OR expires_at > now())",
                session_id, category)
        return "0" not in result

    async def expire_stale(self, *, max_age_hours: int = 24) -> int:
        """Expire working memory entries older than max_age_hours."""
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE sali.working_memory SET expires_at=now() "
                "WHERE expires_at IS NULL AND superseded_by IS NULL "
                "AND updated_at < now() - make_interval(hours => $1)",
                max_age_hours)
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0
