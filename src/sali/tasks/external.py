"""ExternalEntityStore (§11/§12/§34/§35) — external identities/accounts as durable ENTITIES, not memory.

An external account, repo, or service Sali interacts with has a real lifecycle: discovered → planned →
created → verification_pending → verified → configured → active, or degraded/suspended/closed. A
half-created account is never silently abandoned (§13/§35) — it stays visible in a non-terminal state
until it is finished or explicitly closed. Completion is evidence-based: an entity becomes 'verified'
only when its existence/verification is actually confirmed (§12/§41), not when a form was submitted.

Credentials are NEVER stored here (§11/§65) — only a boolean saying a credential is configured in the
existing secret vault. This store records WHAT exists and its state, not any secret.
"""

from __future__ import annotations

import contextlib
from typing import Any
from uuid import UUID, uuid4

from sali.security.redact import redact

_TERMINAL = frozenset(("active", "closed"))


class ExternalEntityStore:
    def __init__(self, pool: Any, publisher: Any = None) -> None:
        self._pool = pool
        self._publisher = publisher

    async def discover(
        self, *, service: str, ref: str | None = None, purpose: str | None = None,
        task_id: UUID | None = None, authority: str | None = None, status: str = "discovered",
        object_type: str = "account",
    ) -> UUID:
        """Record (or return) a DigitalLifeObject Sali will interact with — an account, website, repo,
        domain, project, … (§2). Deduped by (service, ref) so the same object is never tracked twice.
        `ref` is a non-secret identity (username/url/id) and is redacted defensively (§65)."""
        ref_r = redact(ref) if ref else None
        async with self._pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT id FROM external_entity WHERE service=$1 AND coalesce(ref,'')=coalesce($2,'')",
                service, ref_r)
            if existing is not None:
                return UUID(str(existing["id"]))
            eid = uuid4()
            await conn.execute(
                "INSERT INTO external_entity (id, service, ref, purpose, task_id, authority, status, "
                "  object_type) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
                eid, service, ref_r, purpose, task_id, authority, status, object_type)
        await self._emit("digital_object.created", task_id,
                         {"entity_id": str(eid), "service": service, "object_type": object_type,
                          "status": status})
        return eid

    async def advance(self, entity_id: UUID, status: str, *, notes: str | None = None) -> None:
        """Move the entity along its lifecycle. A half-finished account stays in a non-terminal state
        until verification/configuration actually completes (§12)."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE external_entity SET status=$2, notes=coalesce($3, notes), updated_at=now() "
                "WHERE id=$1 RETURNING task_id, service", entity_id, status, notes)
        if row is not None:
            await self._emit("external.updated", row["task_id"],
                             {"entity_id": str(entity_id), "service": row["service"], "status": status})

    async def set_verification(self, entity_id: UUID, *, state: str) -> None:
        """Verification is evidence-based (§41): an entity is 'verified' only when actually confirmed."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE external_entity SET verification_state=$2, "
                "  last_verified = CASE WHEN $2='verified' THEN now() ELSE last_verified END, "
                "  status = CASE WHEN $2='verified' AND status='verification_pending' THEN 'verified' "
                "               ELSE status END, updated_at=now() WHERE id=$1", entity_id, state)

    async def set_credential_configured(self, entity_id: UUID, *, configured: bool = True) -> None:
        """Record ONLY that a credential exists in the vault — never the secret itself (§11/§65)."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE external_entity SET credential_configured=$2, updated_at=now() WHERE id=$1",
                entity_id, configured)

    async def observe(self, entity_id: UUID, *, last_observed: dict[str, Any]) -> None:
        """Record a fresh observation of the object's external state — stamps last_verified so the state
        carries a timestamp, not eternal truth (§17). External state can change between sessions."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE external_entity SET last_observed=$2, last_verified=now(), updated_at=now() "
                "WHERE id=$1 RETURNING task_id, service", entity_id, last_observed)
        if row is not None:
            await self._emit("digital_object.verified", row["task_id"],
                             {"entity_id": str(entity_id), "service": row["service"]})

    async def is_stale(self, entity_id: UUID, *, max_age_seconds: int = 86400) -> bool:
        """Whether the object's remembered external state is too old to trust and should be re-observed
        before acting (§18). Never-observed → stale. Deterministic, from the durable timestamp."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT last_verified, (last_verified < now() - make_interval(secs => $2)) AS old "
                "FROM external_entity WHERE id=$1", entity_id, max_age_seconds)
        if row is None:
            return True
        return row["last_verified"] is None or bool(row["old"])

    async def relate(self, entity_id: UUID, *, rel: str, target: str) -> None:
        """Record a relationship to another digital object (domain hosted_by server, website uses db, …,
        §19). Stored as bounded metadata on the object, not a second graph system."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE external_entity SET relationships = relationships || $2::jsonb, updated_at=now() "
                "WHERE id=$1", entity_id, [{"rel": rel, "target": target}])

    async def get_by_id(self, entity_id: UUID) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, service, ref, object_type, purpose, status, verification_state, "
                "  credential_configured, relationships, last_observed, last_verified "
                "FROM external_entity WHERE id=$1", entity_id)
        return dict(row) if row else None

    async def list_objects(self, *, object_type: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clause = ""
        args: list[Any] = []
        if object_type is not None:
            args.append(object_type)
            clause = " WHERE object_type=$1"
        args.append(limit)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, service, ref, object_type, status, verification_state, last_verified "
                f"FROM external_entity{clause} ORDER BY updated_at DESC LIMIT ${len(args)}", *args)
        return [dict(r) for r in rows]

    async def get(self, *, service: str, ref: str | None = None) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, service, ref, purpose, status, verification_state, credential_configured, "
                "  last_observed FROM external_entity "
                "WHERE service=$1 AND coalesce(ref,'')=coalesce($2,'')",
                service, redact(ref) if ref else None)
        return dict(row) if row else None

    async def unfinished(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """External entities not yet in a terminal state — no half-created account left abandoned (§35)."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, service, ref, purpose, status, verification_state, task_id, created_at "
                "FROM external_entity WHERE status NOT IN ('active','closed') "
                "ORDER BY created_at LIMIT $1", limit)
        return [dict(r) for r in rows]

    async def counts(self) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT status, count(*) AS n FROM external_entity GROUP BY status")
        by = {r["status"]: int(r["n"]) for r in rows}
        unfinished = sum(v for k, v in by.items() if k not in _TERMINAL)
        return {"total": sum(by.values()), "unfinished": unfinished, "by_status": by}

    async def _emit(self, event_type: str, task_id: UUID | None, data: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        with contextlib.suppress(Exception):
            await self._publisher.emit(event_type=event_type, task_id=task_id,
                                       subject_type="external_entity", origin="runtime", data=data)
