"""PersonStore (§23/§24/§25) — generic, evidence-grounded relationship state.

So Sali does not meet the same person as a stranger each time. This holds the durable relationship
SHELL (who, how we relate, preferred channel, last interaction) with provenance + confidence; the
substantive facts continue to live in the existing memory/identity architecture (§23: no duplicate
person-memory system). Every fact is provenance-tagged — a relationship is never asserted as absolute
truth from one conversation (§24), and no sensitive attribute is inferred here.
"""

from __future__ import annotations

import contextlib
from typing import Any
from uuid import UUID, uuid4


class PersonStore:
    def __init__(self, pool: Any, publisher: Any = None) -> None:
        self._pool = pool
        self._publisher = publisher

    async def upsert(
        self, *, name: str, relationship_type: str | None = None, preferred_channel: str | None = None,
        provenance: str = "observed", confidence: float = 0.5,
    ) -> UUID:
        """Record (or refresh) a person. Deduped by name. provenance distinguishes explicitly-told from
        observed / inferred / verified / uncertain (§24/§25) — a stronger provenance never regresses."""
        async with self._pool.acquire() as conn:
            existing = await conn.fetchrow("SELECT id FROM person WHERE name=$1", name)
            if existing is not None:
                await conn.execute(
                    "UPDATE person SET relationship_type=coalesce($2, relationship_type), "
                    "  preferred_channel=coalesce($3, preferred_channel), "
                    "  provenance = CASE WHEN $4='explicit' OR $4='verified' THEN $4 ELSE provenance END, "
                    "  confidence=greatest(confidence, $5), updated_at=now() WHERE id=$1",
                    existing["id"], relationship_type, preferred_channel, provenance, confidence)
                return UUID(str(existing["id"]))
            pid = uuid4()
            await conn.execute(
                "INSERT INTO person (id, name, relationship_type, preferred_channel, provenance, confidence) "
                "VALUES ($1,$2,$3,$4,$5,$6)",
                pid, name, relationship_type, preferred_channel, provenance, confidence)
        await self._emit("relationship.updated", {"person": name, "relationship": relationship_type,
                                                  "provenance": provenance})
        return pid

    async def set_preference(self, name: str, *, key: str, value: Any,
                             provenance: str = "observed") -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE person SET preferences = preferences || $2::jsonb, "
                "  provenance = CASE WHEN $3='explicit' THEN 'explicit' ELSE provenance END, "
                "  updated_at=now() WHERE name=$1",
                name, {key: value, f"{key}__provenance": provenance}, provenance)

    async def record_interaction(self, name: str, *, note: str | None = None) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE person SET last_interaction=now(), context = context || $2::jsonb, updated_at=now() "
                "WHERE name=$1", name, {"last_note": (note or "")[:200]} if note else {})
        await self._emit("social.interaction.recorded", {"person": name})

    async def get(self, name: str) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, name, relationship_type, preferred_channel, preferences, context, "
                "  provenance, confidence, last_interaction FROM person WHERE name=$1", name)
        return dict(row) if row else None

    async def list(self, *, limit: int = 100) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, name, relationship_type, preferred_channel, provenance, confidence, "
                "  last_interaction FROM person ORDER BY last_interaction DESC NULLS LAST LIMIT $1", limit)
        return [dict(r) for r in rows]

    async def counts(self) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            total = int(await conn.fetchval("SELECT count(*) FROM person") or 0)
        return {"total": total}

    async def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        with contextlib.suppress(Exception):
            await self._publisher.emit(event_type=event_type, subject_type="person", origin="runtime",
                                       data=data)
