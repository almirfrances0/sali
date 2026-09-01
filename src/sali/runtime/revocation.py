"""Intent revocation orchestration (§2/§5/§6/§24/§25) — the user's latest clear instruction wins.

When the user abandons an intention, this does the whole propagation deterministically: record a durable
tombstone, cancel every dependent thread of work (pending questions, obligations, commitments, goals,
initiatives) so nothing re-authorizes it, mark the task abandoned, and archive+remove the live row so no
recovery/continuation/watchdog/scheduler path can resurrect it (§6). Historical experience/memory of the
task remains (§3) — only the *current intent* is gone. A revoked intention returns ONLY through an
explicit user revival (§25), recorded as a supersession. Lives in the runtime layer because it
orchestrates across tasks + learning stores.
"""

from __future__ import annotations

import contextlib
from typing import Any
from uuid import UUID


async def revoke_intent(
    pool: Any, task_id: UUID, *, reason: str = "user_revoked", by: str = "user", publisher: Any = None,
) -> dict[str, Any]:
    """Revoke a task the user has abandoned. Tombstone → cancel all dependent work → archive + remove the
    live row. After this, no background path can resurrect it; only an explicit revival can bring it back."""
    from sali.tasks.revocation import RevocationStore
    from sali.tasks.store import TaskStore

    store = TaskStore(pool, publisher)
    task = await store.get(task_id)
    objective = task.objective if task is not None else None

    # 1) durable tombstone FIRST — before archival removes the live row (§4)
    await RevocationStore(pool, publisher).record(task_id=task_id, objective=objective, reason=reason,
                                                  revoked_by=by)
    # 2) cancellation propagation across every dependent thread of work (§5)
    cancelled = await _propagate_cancellation(pool, task_id)
    # 3) mark abandoned + archive (experience preserved) + remove the live row → unresurrectable
    if task is not None:
        with contextlib.suppress(Exception):
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE task SET status='abandoned', is_primary=false, updated_at=now() WHERE id=$1",
                    task_id)
            await store._archive_and_cleanup(task_id)
    if publisher is not None:
        with contextlib.suppress(Exception):
            await publisher.emit(event_type="intent.revocation_propagated", task_id=task_id,
                                 subject_type="task", origin="runtime",
                                 data={"task_id": str(task_id), "cancelled": cancelled})
    return {"revoked": True, "task_id": str(task_id), "cancelled": cancelled}


async def _propagate_cancellation(pool: Any, task_id: UUID) -> dict[str, int]:
    """Cancel every dependent thread of work for a revoked task (§5). Best-effort, deterministic SQL."""
    tid = str(task_id)
    counts: dict[str, int] = {}
    async with pool.acquire() as conn:
        for table, sql in (
            ("pending_question",
             "UPDATE pending_question SET status='cancelled', answered_at=now() "
             "WHERE task_id=$1 AND status='waiting' RETURNING id"),
            ("obligation",
             "UPDATE obligation SET status='cancelled', resolved_at=now() "
             "WHERE task_id=$1 AND status IN ('open','in_progress','blocked') RETURNING id"),
            ("commitment",
             "UPDATE commitment SET status='cancelled', updated_at=now() "
             "WHERE task_id=$1 AND status IN ('open','in_progress','blocked') RETURNING id"),
            ("goal",
             "UPDATE goal SET status='cancelled', updated_at=now() "
             "WHERE task_id=$1 AND status IN ('open','active','blocked') RETURNING id"),
            ("initiative",
             "UPDATE initiative SET status='dismissed', resolved_at=now() "
             "WHERE subject_ref=$1 AND status NOT IN ('completed','dismissed','expired') RETURNING id"),
        ):
            with contextlib.suppress(Exception):
                rows = await conn.fetch(sql, tid if table == "initiative" else task_id)
                counts[table] = len(rows)
    return counts


async def is_resumable(pool: Any, task_id: UUID) -> bool:
    """Whether a task may be resumed — False if the user revoked it and hasn't explicitly revived it (§24).
    Recovery/continuation/initiative paths check this before treating a task as current authorization."""
    from sali.tasks.revocation import RevocationStore
    return not await RevocationStore(pool).is_revoked(task_id)


async def revive_intent(pool: Any, revoked_task_id: UUID, *, new_task_id: UUID, publisher: Any = None) -> None:
    """The user explicitly revived a previously-revoked intention (§25). Links the new task to the
    tombstone so the old one no longer blocks; the historical record is preserved."""
    from sali.tasks.revocation import RevocationStore
    await RevocationStore(pool, publisher).mark_superseded(revoked_task_id, new_task_id=new_task_id)
