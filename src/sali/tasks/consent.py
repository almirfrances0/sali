"""ConsentStore + natural-language consent resolution (§4/§5/§57/§58/§59).

Natural consent is structured state underneath, natural language on top. When an action's judgment level
calls for it (§8), Sali records a ConsentRequest (action, scope, consequence, rationale) and asks the
user in plain language. The user's FREE-TEXT reply is the consent signal — "yes, do it" / "only the first
two" / "not now" / "leave it" / "go ahead with the safe option" — never a y/n gate (§4/§78). Consent is
SCOPED (§5), can EXPIRE (§58), and a parent consent can be inherited by subordinate actions (§59). A
standing authorization ("you don't need to ask before X") is a durable, revocable grant (§38/§39).
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4


@dataclass(slots=True)
class ConsentOutcome:
    status: str                 # granted | modified | declined | deferred | unclear
    note: str | None = None     # the operative clause (e.g. the narrowed scope) for a modified consent


# Ordered patterns — deferred BEFORE declined (so "not now" ≠ "no"), modified BEFORE granted. Deterministic.
_DEFERRED = re.compile(
    r"\b(not now|not yet|later|for now|wait|hold (?:on|off)|in a (?:bit|moment)|maybe later)\b")
_DECLINED = re.compile(r"\b(no|don'?t|do not|leave it|cancel|skip it|stop|never mind|forget it|nope)\b")
_MODIFIED = re.compile(r"\b(only|just the|just do|instead|the other|the safe|safer|rather|except)\b")
_GRANTED = re.compile(r"\b(yes|yeah|yep|sure|ok|okay|go ahead|do it|proceed|please do|sounds good|"
                      r"go for it|confirmed|approved)\b")


def interpret_consent_response(text: str) -> ConsentOutcome:
    """Turn a natural-language reply into a structured consent outcome (§4/§17). Deterministic; ordered so
    'not now' defers rather than declines and 'only the first two' modifies rather than grants. An
    unrecognised reply is 'unclear' — the caller keeps the consent pending and can ask again (§71)."""
    low = (text or "").strip().lower()
    if not low:
        return ConsentOutcome("unclear")
    if _DEFERRED.search(low):
        return ConsentOutcome("deferred")
    if _MODIFIED.search(low):
        return ConsentOutcome("modified", note=text.strip()[:200])
    if _DECLINED.search(low):
        return ConsentOutcome("declined")
    if _GRANTED.search(low):
        return ConsentOutcome("granted")
    return ConsentOutcome("unclear")


class ConsentStore:
    def __init__(self, pool: Any, publisher: Any = None) -> None:
        self._pool = pool
        self._publisher = publisher

    async def request(
        self, *, action: str, scope: str | None = None, consequence: str | None = None,
        rationale: str | None = None, judgment_level: str = "consent", capability: str | None = None,
        reversible: bool | None = None, external: bool | None = None, task_id: UUID | None = None,
        run_id: UUID | None = None, expires_at: datetime | None = None, standing: bool = False,
        parent_consent: UUID | None = None,
    ) -> UUID:
        """Record a pending consent request. The runtime asks the user naturally; this is the structured
        state behind that ask (§57)."""
        cid = uuid4()
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO consent_request (id, task_id, run_id, action, capability, scope, consequence, "
                "  rationale, judgment_level, reversible, external, standing, expires_at, parent_consent) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)",
                cid, task_id, run_id, action, capability, scope, consequence, rationale, judgment_level,
                reversible, external, standing, expires_at, parent_consent)
        await self._emit("consent.requested", task_id,
                         {"consent_id": str(cid), "action": action[:160], "scope": scope,
                          "judgment_level": judgment_level})
        return cid

    async def pending(self, task_id: UUID) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, action, scope, consequence, judgment_level FROM consent_request "
                "WHERE task_id=$1 AND status='pending' ORDER BY created_at DESC LIMIT 1", task_id)
        return dict(row) if row else None

    async def resolve(self, consent_id: UUID, response: str) -> ConsentOutcome:
        """Resolve a pending consent from a natural-language reply. Grant/modify/decline/defer per the
        interpreted outcome; 'unclear' leaves it pending. A granted standing consent becomes a durable
        authorization; a granted one-off authorizes just its own scope (§5/§58)."""
        outcome = interpret_consent_response(response)
        if outcome.status == "unclear":
            return outcome
        status = outcome.status
        granted_scope: str | None = None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT scope, task_id, standing FROM consent_request WHERE id=$1",
                                      consent_id)
            if row is None:
                return ConsentOutcome("unclear")
            if status in ("granted", "modified"):
                granted_scope = outcome.note if status == "modified" else row["scope"]
            await conn.execute(
                "UPDATE consent_request SET status=$2, response=$3, granted_scope=$4, resolved_at=now() "
                "WHERE id=$1 AND status='pending'", consent_id, status, response[:500], granted_scope)
        event = {"granted": "consent.granted", "modified": "consent.modified",
                 "declined": "consent.revoked", "deferred": "consent.requested"}.get(status, "consent.requested")
        await self._emit(event, row["task_id"],
                         {"consent_id": str(consent_id), "status": status, "granted_scope": granted_scope})
        return outcome

    async def resolve_pending_for_task(self, task_id: UUID, response: str) -> ConsentOutcome | None:
        """If a consent is pending for the task, resolve it from the user's reply. Returns the outcome, or
        None if nothing was pending (so ordinary conversation isn't mistaken for a consent reply)."""
        pend = await self.pending(task_id)
        if pend is None:
            return None
        return await self.resolve(UUID(str(pend["id"])), response)

    async def has_standing_authorization(self, *, scope: str, now: datetime | None = None) -> bool:
        """Whether a durable standing authorization currently covers this scope (§38) — not expired,
        not revoked."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchval(
                "SELECT 1 FROM consent_request WHERE standing AND scope=$1 "
                "  AND status IN ('granted','modified') "
                "  AND (expires_at IS NULL OR expires_at > coalesce($2, now())) LIMIT 1", scope, now)
        return bool(row)

    async def grant_standing(self, *, scope: str, action: str, task_id: UUID | None = None) -> UUID:
        """Record a durable standing authorization from an explicit user preference (§38)."""
        cid = await self.request(action=action, scope=scope, judgment_level="normal",
                                 standing=True, task_id=task_id)
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE consent_request SET status='granted', granted_scope=$2, resolved_at=now() "
                "WHERE id=$1", cid, scope)
        await self._emit("consent.granted", task_id, {"consent_id": str(cid), "scope": scope,
                                                      "standing": True})
        return cid

    async def revoke_standing(self, *, scope: str) -> int:
        """Revoke standing authorizations for a scope (§39: "don't do that anymore")."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "UPDATE consent_request SET status='revoked', resolved_at=now() "
                "WHERE standing AND scope=$1 AND status IN ('granted','modified') RETURNING id", scope)
        for r in rows:
            await self._emit("consent.revoked", None, {"consent_id": str(r["id"]), "scope": scope})
        return len(rows)

    async def expire_stale(self, *, now: datetime | None = None) -> int:
        """Expire pending/granted consents past their expiry (§58) — expired consent is never reused."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "UPDATE consent_request SET status='expired', resolved_at=now() "
                "WHERE status IN ('pending','granted','modified') AND expires_at IS NOT NULL "
                "  AND expires_at <= coalesce($1, now()) RETURNING id", now)
        for r in rows:
            await self._emit("consent.expired", None, {"consent_id": str(r["id"])})
        return len(rows)

    async def get(self, consent_id: UUID) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, action, scope, consequence, rationale, judgment_level, status, response, "
                "  granted_scope, standing, expires_at FROM consent_request WHERE id=$1", consent_id)
        return dict(row) if row else None

    async def open(self, *, limit: int = 50) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, task_id, action, scope, consequence, judgment_level, created_at "
                "FROM consent_request WHERE status='pending' ORDER BY created_at DESC LIMIT $1", limit)
        return [dict(r) for r in rows]

    async def counts(self) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT status, count(*) AS n FROM consent_request GROUP BY status")
        by = {r["status"]: int(r["n"]) for r in rows}
        return {"pending": by.get("pending", 0), "by_status": by}

    async def _emit(self, event_type: str, task_id: UUID | None, data: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        with contextlib.suppress(Exception):
            await self._publisher.emit(event_type=event_type, task_id=task_id, subject_type="consent",
                                       origin="runtime", data=data)
