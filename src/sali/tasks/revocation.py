"""RevocationStore (§4/§24/§25) — the durable intent tombstone.

When the user abandons an intention ("leave that project", "forget it"), the task is archived and its
live row removed, so recovery/continuation/watchdog can never resurrect it. This store keeps the durable
EVIDENCE that the intention was revoked — for audit, and so a later message can be recognised as a
REVIVAL of a known-abandoned task rather than silently re-authorizing it. A revoked intention returns to
active life ONLY through a new explicit user intention (§25), which is recorded as a supersession here.
Historical memory/experience of the task remains; the tombstone only says "no longer a current intent."
"""

from __future__ import annotations

import contextlib
import re
from typing import Any
from uuid import UUID, uuid4

# Clear abandonment phrases (§1). Deterministic + conservative: requires an abandonment verb tied to the
# work — a bare "stop"/"wait" is an INTERRUPT (Prompt 2 attention), not a revocation, so it is NOT matched.
_REVOKE = re.compile(
    r"\b(forget (?:about )?(?:that|it|this|the)\b"
    r"|leave (?:that|it|this|the)\b.*\b(?:task|project|alone|for good|for now)"
    r"|abandon (?:that|it|this|the)\b"
    r"|cancel (?:that|this|the)\b.*\b(?:task|project|work|it)"
    r"|drop (?:that|it|this|the)\b.*\b(?:task|project|idea)"
    r"|stop working on (?:that|it|this|the)\b"
    r"|don'?t (?:want|need) (?:that|it|this|the)\b.*\banymore"
    r"|(?:i )?don'?t want (?:that|it|this) anymore"
    r"|scrap (?:that|it|this|the)\b"
    # Bare forms. The patterns above all require a qualifier after the pronoun ("leave it ALONE",
    # "cancel that TASK"), so the way people actually call work off — "leave it.", "never mind." —
    # matched nothing and the intent stayed live for recovery to pick back up.
    r"|^\s*(?:leave|drop|scrap|forget)\s+(?:it|that|this)\s*[.!]*$"
    r"|^\s*(?:never\s*mind|nevermind)\s*[.!]*$)")


# The same words with a negation in front mean the OPPOSITE, and the patterns above are unanchored, so
# "don't forget it" matched "forget it" and abandoned the very work Almir was asking Sali to hold on to.
# A revocation is irreversible from the conversation's point of view, so this direction of error is the
# expensive one.
_NEGATED = re.compile(r"\b(?:don'?t|do\s+not|never|dont)\s+(?:you\s+)?"
                      r"(?:forget|leave|drop|scrap|abandon|cancel|stop)\b")


def classify_revocation(message: str) -> bool:
    """True when the user is clearly ABANDONING the current work (not merely interrupting it, §1). The
    caller revokes the active task's intent on a match. Conservative — most messages return False."""
    text = (message or "").strip().lower()
    if not text or len(text) >= 400 or _NEGATED.search(text):
        return False
    return bool(_REVOKE.search(text))


class RevocationStore:
    def __init__(self, pool: Any, publisher: Any = None) -> None:
        self._pool = pool
        self._publisher = publisher

    async def record(
        self, *, task_id: UUID, objective: str | None = None, reason: str = "user_revoked",
        revoked_by: str = "user",
    ) -> UUID:
        """Tombstone an abandoned intention. Idempotent per task_id."""
        rid = uuid4()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO revoked_intent (id, task_id, objective, reason, revoked_by) "
                "VALUES ($1,$2,$3,$4,$5) ON CONFLICT (task_id) DO UPDATE SET reason=excluded.reason, "
                "  revoked_at=now() RETURNING id", rid, task_id, objective, reason, revoked_by)
        await self._emit("intent.revoked", task_id,
                         {"task_id": str(task_id), "reason": reason, "objective": (objective or "")[:120]})
        return UUID(str(row["id"]))

    async def is_revoked(self, task_id: UUID) -> bool:
        """Whether this task was revoked by the user and not since revived — recovery/initiative MUST
        check this before treating any historical task reference as current authorization (§24)."""
        async with self._pool.acquire() as conn:
            return bool(await conn.fetchval(
                "SELECT 1 FROM revoked_intent WHERE task_id=$1 AND superseded_by IS NULL", task_id))

    async def mark_superseded(self, task_id: UUID, *, new_task_id: UUID) -> None:
        """The user explicitly revived a revoked intention (§25) — link the new task; the old tombstone
        stays for audit but no longer blocks."""
        async with self._pool.acquire() as conn:
            await conn.execute("UPDATE revoked_intent SET superseded_by=$2 WHERE task_id=$1",
                               task_id, new_task_id)
        await self._emit("intent.revocation_propagated", task_id,
                         {"task_id": str(task_id), "superseded_by": str(new_task_id), "revived": True})

    async def get(self, task_id: UUID) -> dict[str, Any] | None:
        """The tombstone for one task, if any — carries the objective needed to revive it (§25)."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT task_id, objective, reason, revoked_by, revoked_at, superseded_by "
                "FROM revoked_intent WHERE task_id=$1", task_id)
        return dict(row) if row else None

    async def recent(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Recently-revoked intentions — a later message can be matched against these to detect a revival."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT task_id, objective, reason, revoked_at, superseded_by FROM revoked_intent "
                "WHERE superseded_by IS NULL ORDER BY revoked_at DESC LIMIT $1", limit)
        return [dict(r) for r in rows]

    async def counts(self) -> dict[str, int]:
        async with self._pool.acquire() as conn:
            active = int(await conn.fetchval(
                "SELECT count(*) FROM revoked_intent WHERE superseded_by IS NULL") or 0)
        return {"active_tombstones": active}

    async def _emit(self, event_type: str, task_id: UUID, data: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        with contextlib.suppress(Exception):
            await self._publisher.emit(event_type=event_type, task_id=task_id, subject_type="task",
                                       origin="runtime", data=data)
