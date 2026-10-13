"""Task-associated web research + evidence-aware learning candidates (Prompt 5 §16-20).

Just-in-time research that Sali does WHILE working is durable EVIDENCE tied to the task/run/step — not
temporary conversation context, and never automatically permanent truth (§17). A research finding that
is actually applied and then VERIFIED by real completion evidence can become a learning CANDIDATE; a
candidate is promoted to durable memory only once verified (§18/§19). A failed experiment never
auto-promotes. Pure persistence here — the web fetch and the memory write live above the tasks layer.
"""

from __future__ import annotations

import contextlib
from typing import Any
from uuid import UUID, uuid4

from sali.obs.log import get_logger

log = get_logger("sali.tasks.research")


class ResearchStore:
    def __init__(self, pool: Any, publisher: Any = None) -> None:
        self._pool = pool
        self._publisher = publisher

    # ── research (durable evidence) ─────────────────────────────────────────────────────────────────
    async def record_research(
        self, *, task_id: UUID | None, run_id: UUID | None, step_seq: int | None,
        query: str, source: str | None, summary: str, confidence: float,
        content_hash: str | None, status: str = "completed",
    ) -> UUID:
        rid = uuid4()
        if task_id is None:
            return rid  # no active task — nothing durable to attach it to
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO task_research "
                "  (id, task_id, run_id, step_seq, query, source, summary, confidence, content_hash, status) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
                rid, task_id, run_id, step_seq, query, source, summary, confidence,
                content_hash, status)
        await self._emit(
            "research.failed" if status == "failed" else "research.completed", task_id, run_id,
            {"research_id": str(rid), "query": query[:200], "source": source,
             "confidence": confidence})
        return rid

    async def list_research(self, task_id: UUID, *, limit: int = 20) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, run_id, step_seq, query, source, summary, confidence, used, status, "
                "  retrieved_at FROM task_research WHERE task_id = $1 "
                "ORDER BY retrieved_at DESC LIMIT $2", task_id, limit)
        return [dict(r) for r in rows]

    async def mark_used(self, research_id: UUID, task_id: UUID | None = None,
                        run_id: UUID | None = None) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE task_research SET used = true WHERE id = $1", research_id)
        await self._emit("research.used", task_id, run_id, {"research_id": str(research_id)})

    def render(self, research: list[dict[str, Any]], *, per_item_chars: int = 300,
               max_items: int = 4) -> str:
        """A bounded block of the task's recent research findings for continuation (§13/§24)."""
        if not research:
            return ""
        parts = ["RESEARCH FINDINGS (evidence Sali gathered for this task — guidance, not proof of "
                 "completion; verify by running things):"]
        for r in research[:max_items]:
            summ = (r.get("summary") or "").strip()
            if len(summ) > per_item_chars:
                summ = summ[:per_item_chars].rstrip() + " …"
            src = f" ({r['source']})" if r.get("source") else ""
            parts.append(f"- Q: {(r.get('query') or '')[:120]}\n  A: {summ}{src}")
        return "\n".join(parts)

    # ── learning candidates (evidence-aware promotion, §19) ─────────────────────────────────────────
    async def record_candidate(
        self, *, task_id: UUID | None, run_id: UUID | None, lesson: str,
        source: str | None = None, research_id: UUID | None = None, confidence: float = 0.5,
    ) -> UUID:
        cid = uuid4()
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO learning_candidate "
                "  (id, task_id, run_id, lesson, source, research_id, verification_state, confidence) "
                "VALUES ($1,$2,$3,$4,$5,$6,'unverified',$7)",
                cid, task_id, run_id, lesson, source, research_id, confidence)
        await self._emit("learning.candidate_created", task_id, run_id,
                         {"candidate_id": str(cid), "lesson": lesson[:200],
                          "verification_state": "unverified"})
        return cid

    async def list_candidates(
        self, *, task_id: UUID | None = None, verification_state: str | None = None,
        promoted: bool | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        if task_id is not None:
            args.append(task_id)
            clauses.append(f"task_id = ${len(args)}")
        if verification_state is not None:
            args.append(verification_state)
            clauses.append(f"verification_state = ${len(args)}")
        if promoted is not None:
            args.append(promoted)
            clauses.append(f"promoted = ${len(args)}")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT id, task_id, run_id, lesson, source, research_id, verification_state, "
                f"  confidence, promoted, created_at FROM learning_candidate{where} "
                f"ORDER BY created_at", *args)
        return [dict(r) for r in rows]

    async def verify_task_candidates(self, task_id: UUID) -> int:
        """Mark a passing task's unverified candidates as VERIFIED (called when the reviewer PASSES the
        task — real completion evidence backs them). Returns how many were verified. A task that never
        passes leaves its candidates 'unverified', so they can never be promoted (§21)."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "UPDATE learning_candidate SET verification_state = 'verified' "
                "WHERE task_id = $1 AND verification_state = 'unverified' RETURNING id", task_id)
        return len(rows)

    async def promotable_candidates(self) -> list[dict[str, Any]]:
        """Verified, not-yet-promoted candidates — ready to become durable memory."""
        return await self.list_candidates(verification_state="verified", promoted=False)

    async def mark_promoted(self, candidate_id: UUID, *, task_id: UUID | None = None) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE learning_candidate SET promoted = true, promoted_at = now() WHERE id = $1",
                candidate_id)
        await self._emit("learning.promoted", task_id, None, {"candidate_id": str(candidate_id)})

    async def _emit(
        self, event_type: str, task_id: UUID | None, run_id: UUID | None, data: dict[str, Any],
    ) -> None:
        if self._publisher is None:
            return
        with contextlib.suppress(Exception):
            await self._publisher.emit(
                event_type=event_type, task_id=task_id, run_id=run_id,
                subject_type="task", subject_id=task_id, origin="research", data=data)
