"""LearningCandidateStore (Prompt 7 §3-§13) — the evidence-backed lifecycle of a learning candidate.

A candidate travels: observed → (outcomes) → supported → verified → promoted → active, or is rejected /
superseded. Confidence is DERIVED from evidence (never invented); duplicate observations MERGE rather
than multiply (§13); a genuine claim conflict is RECORDED and resolved by evidence priority, never
silently overwritten (§11). Promotion writes a durable, reusable memory — the bridge from task-local
experience to general knowledge — and is gated by the evidence rules in ``evidence.py`` (§21/§22).

This sits in the learning layer: it may write durable ``memory`` (below it) and emit events, but it
never touches a core invariant (workspace/lease/reviewer/single-primary). Learning influences
decisions; it does not control the kernel (§17/§37).
"""

from __future__ import annotations

import contextlib
from typing import Any
from uuid import UUID, uuid4

from sali.core.enums import MemoryLayer, MemorySource
from sali.learning import evidence
from sali.memory import writer as memory_writer
from sali.obs.log import get_logger

log = get_logger("sali.learning.candidates")


class LearningCandidateStore:
    def __init__(self, pool: Any, publisher: Any = None) -> None:
        self._pool = pool
        self._publisher = publisher

    # ── observe: create or merge a candidate (§3/§13) ───────────────────────────────────────────────
    async def observe(
        self, *, lesson: str, scope: str = "task", scope_ref: str | None = None,
        source_type: str | None = None, evidence_level: int = 0, task_id: UUID | None = None,
        run_id: UUID | None = None, source: str | None = None, research_id: UUID | None = None,
        claim_key: str | None = None, claim_value: str | None = None,
    ) -> UUID:
        """Record an observation. Deduped by (scope, scope_ref, content-hash): re-observing a known
        lesson MERGES — bumps its observation count and raises its evidence level — instead of creating
        a duplicate. A fresh lesson is inserted at state 'observed'. Confidence is always derived."""
        h = evidence.content_hash(lesson, scope, scope_ref)
        async with self._pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT id, evidence_level, times_observed, times_successful, times_failed "
                "FROM learning_candidate WHERE scope=$1 AND coalesce(scope_ref,'')=coalesce($2,'') "
                "  AND content_hash=$3 AND verification_state NOT IN ('superseded','rejected') "
                "LIMIT 1", scope, scope_ref, h)
            if existing is not None:
                new_level = max(int(existing["evidence_level"]), int(evidence_level))
                conf = evidence.derive_confidence(
                    evidence_level=new_level, times_successful=existing["times_successful"],
                    times_failed=existing["times_failed"], times_observed=existing["times_observed"] + 1)
                await conn.execute(
                    "UPDATE learning_candidate SET times_observed = times_observed + 1, "
                    "  evidence_level = $2, confidence = $3, verification_state = "
                    "  CASE WHEN verification_state='unverified' THEN 'observed' ELSE verification_state END, "
                    "  updated_at = now() WHERE id = $1",
                    existing["id"], new_level, conf)
                await self._emit("learning.candidate_updated", task_id, run_id,
                                 {"candidate_id": str(existing["id"]), "times_observed_bumped": True})
                return UUID(str(existing["id"]))

            cid = uuid4()
            conf = evidence.derive_confidence(evidence_level=evidence_level, times_observed=1)
            await conn.execute(
                "INSERT INTO learning_candidate "
                "  (id, task_id, run_id, lesson, source, research_id, scope, scope_ref, source_type, "
                "   evidence_level, verification_state, confidence, content_hash, claim_key, claim_value) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'observed',$11,$12,$13,$14)",
                cid, task_id, run_id, lesson, source, research_id, scope, scope_ref, source_type,
                evidence_level, conf, h, claim_key, claim_value)
        await self._emit("learning.observed", task_id, run_id,
                         {"candidate_id": str(cid), "scope": scope, "evidence_level": evidence_level})
        await self._emit("learning.candidate_created", task_id, run_id,
                         {"candidate_id": str(cid), "lesson": lesson[:200], "scope": scope})
        if claim_key is not None:
            await self.detect_contradiction(cid)
        return cid

    # ── outcomes: real success/failure moves the evidence (§4/§5) ────────────────────────────────────
    async def record_outcome(self, candidate_id: UUID, *, success: bool) -> None:
        """A candidate's guidance was applied and it succeeded or failed. Bumps the counter, recomputes
        confidence, and moves the state — a success at repeated-success level becomes 'supported'; a
        failure never becomes 'verified' (§5). Raises the evidence level on repeated success."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT evidence_level, times_successful, times_failed, verification_state "
                "FROM learning_candidate WHERE id=$1", candidate_id)
            if row is None:
                return
            succ = row["times_successful"] + (1 if success else 0)
            fail = row["times_failed"] + (0 if success else 1)
            level = int(row["evidence_level"])
            state = row["verification_state"]
            if success:
                level = max(level, int(evidence.EvidenceLevel.TOOL_SUCCESS))
                if succ >= 2:
                    level = max(level, int(evidence.EvidenceLevel.REPEATED_SUCCESS))
                if state in ("unverified", "observed", "attempted"):
                    state = "supported"
            elif state in ("unverified", "observed"):
                state = "attempted"
            conf = evidence.derive_confidence(
                evidence_level=level, times_successful=succ, times_failed=fail, times_observed=1)
            await conn.execute(
                "UPDATE learning_candidate SET times_successful=$2, times_failed=$3, evidence_level=$4, "
                "  verification_state=$5, confidence=$6, updated_at=now() WHERE id=$1",
                candidate_id, succ, fail, level, state, conf)
        await self._emit("learning.candidate_updated", None, None,
                         {"candidate_id": str(candidate_id), "success": success})

    async def verify_from_reviewer(self, task_id: UUID) -> int:
        """A reviewer PASS is the strongest evidence (§4 level 5). Mark the task's not-yet-terminal
        candidates verified and raise them to REVIEWER_VERIFIED — now (and only now) promotable."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "UPDATE learning_candidate SET verification_state='verified', "
                "  evidence_level = greatest(evidence_level, $2), "
                "  confidence = greatest(confidence, 0.9), updated_at=now() "
                "WHERE task_id=$1 AND verification_state IN "
                "  ('unverified','observed','attempted','supported') RETURNING id",
                task_id, int(evidence.EvidenceLevel.REVIEWER_VERIFIED))
        for r in rows:
            await self._emit("learning.verified", task_id, None, {"candidate_id": str(r["id"])})
        return len(rows)

    # ── contradiction: record + resolve by evidence priority, never overwrite (§11) ──────────────────
    async def detect_contradiction(self, candidate_id: UUID) -> UUID | None:
        """If this candidate asserts a claim (claim_key) that conflicts with a standing candidate's
        value in the same scope, RECORD the contradiction and resolve it by evidence priority: stronger
        evidence wins (the weaker is superseded/marked contradicted), equal evidence is left for review.
        The old knowledge is never silently overwritten. Returns the contradiction id, or None."""
        async with self._pool.acquire() as conn:
            new = await conn.fetchrow(
                "SELECT id, scope, scope_ref, claim_key, claim_value, lesson, evidence_level "
                "FROM learning_candidate WHERE id=$1", candidate_id)
            if new is None or new["claim_key"] is None or new["claim_value"] is None:
                return None
            prior = await conn.fetchrow(
                "SELECT id, claim_value, lesson, evidence_level FROM learning_candidate "
                "WHERE scope=$1 AND coalesce(scope_ref,'')=coalesce($2,'') AND claim_key=$3 "
                "  AND id <> $4 AND claim_value IS DISTINCT FROM $5 "
                "  AND verification_state NOT IN ('superseded','rejected') "
                "ORDER BY evidence_level DESC, created_at ASC LIMIT 1",
                new["scope"], new["scope_ref"], new["claim_key"], candidate_id, new["claim_value"])
            if prior is None:
                return None

            new_lvl, old_lvl = int(new["evidence_level"]), int(prior["evidence_level"])
            if new_lvl > old_lvl:
                status, resolution, loser = "resolved", "new evidence outranks the standing claim", prior["id"]
            elif old_lvl > new_lvl:
                status, resolution, loser = "resolved", "standing claim has stronger evidence", new["id"]
            else:
                status, resolution, loser = "needs_review", None, None

            cxid = uuid4()
            await conn.execute(
                "INSERT INTO learning_contradiction "
                "  (id, scope, scope_ref, claim_key, old_id, new_id, old_claim, new_claim, status, resolution) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
                cxid, new["scope"], new["scope_ref"], new["claim_key"], prior["id"], new["id"],
                prior["lesson"], new["lesson"], status, resolution)
            if loser is not None:
                # the weaker claim is retired as contradicted — recorded, not deleted (auditable)
                await conn.execute(
                    "UPDATE learning_candidate SET verification_state='contradicted', updated_at=now() "
                    "WHERE id=$1", loser)
                await conn.execute(
                    "UPDATE learning_contradiction SET resolved_at=now() WHERE id=$1", cxid)
        await self._emit("learning.contradicted", None, None,
                         {"contradiction_id": str(cxid), "status": status, "claim_key": new["claim_key"]})
        return cxid

    # ── promotion: earned knowledge becomes durable + reusable (§12/§21) ─────────────────────────────
    async def promotable(self) -> list[dict[str, Any]]:
        """Verified, not-yet-promoted candidates that clear the evidence bar (§21/§22)."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, task_id, lesson, source, scope, scope_ref, source_type, evidence_level, "
                "  verification_state, times_successful, times_failed, times_observed, claim_value "
                "FROM learning_candidate WHERE promoted=false "
                "  AND verification_state IN ('verified','supported')")
        out: list[dict[str, Any]] = []
        for r in rows:
            if evidence.is_promotable(
                evidence_level=r["evidence_level"], verification_state=r["verification_state"],
                source_type=r["source_type"], times_successful=r["times_successful"],
                times_failed=r["times_failed"], times_observed=r["times_observed"],
                has_explanation=bool(r["claim_value"])):
                out.append(dict(r))
        return out

    async def promote(self, candidate: dict[str, Any]) -> bool:
        """Promote one candidate into durable, reusable memory (semantic, scope-tagged) and mark it
        promoted. Idempotent via the promoted flag. Returns True if it was promoted this call."""
        cid = candidate["id"]
        try:
            async with self._pool.acquire() as conn:
                # re-check under the row so two consolidations don't double-promote
                still = await conn.fetchval(
                    "SELECT 1 FROM learning_candidate WHERE id=$1 AND promoted=false", cid)
                if not still:
                    return False
                await memory_writer.remember(
                    conn, layer=MemoryLayer.SEMANTIC, content=candidate["lesson"],
                    source=MemorySource.INFERENCE, functional=True,
                    claim_key=f"lesson:{cid}", importance=0.55,
                    obs_conf=evidence.derive_confidence(
                        evidence_level=candidate["evidence_level"],
                        times_successful=candidate["times_successful"],
                        times_failed=candidate["times_failed"],
                        times_observed=candidate["times_observed"]),
                    note=f"promoted learning ({candidate['scope']}): {candidate.get('source') or ''}",
                    structured={"kind": "learned_lesson", "candidate_id": str(cid),
                                "scope": candidate["scope"], "scope_ref": candidate.get("scope_ref"),
                                "evidence_level": candidate["evidence_level"], "certainty": "learned"})
                await conn.execute(
                    "UPDATE learning_candidate SET promoted=true, promoted_at=now(), "
                    "  verification_state='promoted', updated_at=now() WHERE id=$1", cid)
        except Exception as exc:  # noqa: BLE001 - a bad promote is skipped, never fatal to a pass
            log.warning("promote_failed", candidate_id=str(cid), error=str(exc))
            return False
        await self._emit("learning.promoted", candidate.get("task_id"), None,
                         {"candidate_id": str(cid), "scope": candidate["scope"]})
        return True

    async def reject(self, candidate_id: UUID, *, reason: str = "") -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE learning_candidate SET verification_state='rejected', updated_at=now() WHERE id=$1",
                candidate_id)
        await self._emit("learning.rejected", None, None,
                         {"candidate_id": str(candidate_id), "reason": reason[:200]})

    async def supersede(self, old_id: UUID, new_id: UUID) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE learning_candidate SET verification_state='superseded', superseded_by=$2, "
                "  updated_at=now() WHERE id=$1", old_id, new_id)
        await self._emit("learning.superseded", None, None,
                         {"old_id": str(old_id), "new_id": str(new_id)})

    # ── reads for retrieval / observability ─────────────────────────────────────────────────────────
    async def active(
        self, *, scope: str | None = None, scope_ref: str | None = None, limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Verified/promoted, non-contradicted candidates — the reusable knowledge, best evidence first."""
        clauses = ["verification_state IN ('verified','promoted','supported')"]
        args: list[Any] = []
        if scope is not None:
            args.append(scope)
            clauses.append(f"scope = ${len(args)}")
        if scope_ref is not None:
            args.append(scope_ref)
            clauses.append(f"coalesce(scope_ref,'') = coalesce(${len(args)},'')")
        args.append(limit)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, lesson, scope, scope_ref, source, source_type, evidence_level, confidence, "
                "  times_successful, times_failed, verification_state, promoted, updated_at "
                f"FROM learning_candidate WHERE {' AND '.join(clauses)} "
                f"ORDER BY evidence_level DESC, confidence DESC, updated_at DESC LIMIT ${len(args)}", *args)
        return [dict(r) for r in rows]

    async def recent(self, *, limit: int = 10) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, lesson, scope, verification_state, evidence_level, confidence, created_at "
                "FROM learning_candidate ORDER BY updated_at DESC LIMIT $1", limit)
        return [dict(r) for r in rows]

    async def counts(self) -> dict[str, Any]:
        """Compact learning-health counts for CognitiveState / the iPhone controller (§28/§32)."""
        async with self._pool.acquire() as conn:
            by_state = {
                r["verification_state"]: r["n"] for r in await conn.fetch(
                    "SELECT verification_state, count(*) AS n FROM learning_candidate "
                    "GROUP BY verification_state")}
            open_cx = int(await conn.fetchval(
                "SELECT count(*) FROM learning_contradiction "
                "WHERE status IN ('open','needs_review')") or 0)
        active = int(by_state.get("verified", 0)) + int(by_state.get("promoted", 0)) \
            + int(by_state.get("supported", 0))
        return {
            "active": active,
            "observed": int(by_state.get("observed", 0)),
            "verified": int(by_state.get("verified", 0)),
            "promoted": int(by_state.get("promoted", 0)),
            "rejected": int(by_state.get("rejected", 0)),
            "contradicted": int(by_state.get("contradicted", 0)),
            "open_contradictions": open_cx,
            "by_state": {k: int(v) for k, v in by_state.items()},
        }

    async def contradictions(self, *, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        clause = ""
        args: list[Any] = []
        if status is not None:
            args.append(status)
            clause = " WHERE status = $1"
        args.append(limit)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, scope, scope_ref, claim_key, old_claim, new_claim, status, resolution, "
                f"  created_at FROM learning_contradiction{clause} ORDER BY created_at DESC LIMIT ${len(args)}",
                *args)
        return [dict(r) for r in rows]

    async def _emit(
        self, event_type: str, task_id: UUID | None, run_id: UUID | None, data: dict[str, Any],
    ) -> None:
        if self._publisher is None:
            return
        with contextlib.suppress(Exception):
            await self._publisher.emit(
                event_type=event_type, task_id=task_id, run_id=run_id,
                subject_type="learning", subject_id=task_id, origin="learning", data=data)
