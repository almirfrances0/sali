"""Lifetime experience (Prompt: lifetime memory §8/§18/§19/§49) — Sali's durable, personally-lived
experience, distilled from a finished task and preserved for the life of the system.

The philosophical shift: a completed task is not the end of the experience. The workspace is disposable;
the experience is permanent. When a task PASSes review, this store reads the DURABLE task graph (the
objective, what was attempted, what FAILED, what SUCCEEDED, the research used, the decisions made, the
reviewer verdict) *before* the workspace is cleaned, and distils it into:

  • one EPISODIC "experience" memory — "I personally went through this" — carrying the whole shape
    (attempts, failures = negative knowledge, success, verification, procedure, provenance); and
  • one PROCEDURAL memory for the *verified* successful sequence — scoped, uncertain, never universal.

It REUSES the existing memory substrate (memory table + memory_evidence + contradiction + pgvector,
migrations 0001/0002/0010) — no parallel memory system (§43). Everything is written through
``memory.writer`` so redaction (§38 secrets), dedup/corroboration (§46), and contradiction-by-evidence
(§47) all apply for free. A failed approach is recorded as negative knowledge but is NEVER written as a
verified capability (§9). Experience is stronger evidence than a webpage: an experience Sali actually
verified is sourced as a SYSTEM_OBSERVATION (§8/§10).
"""

from __future__ import annotations

import contextlib
import hashlib
from enum import StrEnum
from typing import Any
from uuid import UUID

from sali.core.enums import MemoryLayer, MemorySource
from sali.memory import writer as memory_writer
from sali.obs.log import get_logger

log = get_logger("sali.learning.experience")

_MAX_FAILURES = 8
_MAX_ITEMS = 6


class EvidenceState(StrEnum):
    """The experiential evidence ladder (§8/§10) — how strongly Sali has *lived* a claim.
    DOCUMENTED (a source said so) is weaker than SUCCESSFUL (Sali did it) which is weaker than
    VERIFIED (a check confirmed it) / CONFIRMED (the user confirmed it). CONTRADICTED = later disproved."""

    DOCUMENTED = "documented"
    OBSERVED = "observed"
    ATTEMPTED = "attempted"
    SUCCESSFUL = "successful"
    VERIFIED = "verified"
    CONFIRMED = "confirmed"
    CONTRADICTED = "contradicted"


class ExperienceStore:
    def __init__(self, pool: Any, publisher: Any = None, provider: Any = None) -> None:
        self._pool = pool
        self._publisher = publisher
        # Optional, and only ever used to NAME a capability (see `_capability_name`). Read paths — the
        # API's read-only routes — construct this store without one and behave exactly as before.
        self._provider = provider

    # ── experience extraction on task PASS, before cleanup (§18/§19) ─────────────────────────────────
    async def extract_and_persist(self, task_id: UUID) -> dict[str, Any] | None:
        """Distil a finished task into durable experience. Reads the task graph (must run BEFORE the
        workspace/task is cleaned), writes the episodic experience + a verified procedural memory, and
        returns the record. Returns None if the task is gone. Idempotent via content-hash dedup (§46)."""
        async with self._pool.acquire() as conn:
            task = await conn.fetchrow(
                "SELECT id, objective, workspace_root, status, result FROM task WHERE id=$1",
                task_id)
            if task is None:
                return None
            execs = await conn.fetch(
                "SELECT step_seq, tool_name, status, result_summary, error, attempt "
                "FROM task_execution WHERE task_id=$1 ORDER BY started_at", task_id)
            research = await conn.fetch(
                "SELECT query, source, summary FROM task_research WHERE task_id=$1 "
                "ORDER BY retrieved_at LIMIT $2", task_id, _MAX_ITEMS)
            decisions = await conn.fetch(
                "SELECT decision, reason FROM task_decision WHERE task_id=$1 AND status='active' "
                "ORDER BY created_at LIMIT $2", task_id, _MAX_ITEMS)
            lesson_row = await conn.fetchrow(
                "SELECT lesson FROM learning_candidate WHERE task_id=$1 "
                "ORDER BY evidence_level DESC, created_at DESC LIMIT 1", task_id)

        verified = task["status"] == "done"    # the hook fires on a reviewer PASS
        record = _build_record(task, execs, research, decisions,
                               lesson=lesson_row["lesson"] if lesson_row else None, verified=verified)

        # 1) the EPISODIC experience — "I personally went through this" (§8). Sali's own verified
        #    experience is a direct observation, so it outranks a mere documented claim (§10).
        content = record["headline"]
        async with self._pool.acquire() as conn:
            mem = await memory_writer.remember(
                conn, layer=MemoryLayer.EPISODIC, content=content,
                source=MemorySource.SYSTEM_OBSERVATION if verified else MemorySource.INFERENCE,
                importance=0.7 if verified else 0.5, source_ref=task_id,
                scope=record["scope_ref"] or "global",
                structured={k: v for k, v in record.items() if k != "headline"})
        record["memory_id"] = str(mem.id)
        await self._emit("experience.recorded", task_id,
                         {"memory_id": str(mem.id), "objective": record["objective"][:120],
                          "evidence_state": record["evidence_state"], "failures": len(record["failures"])})
        if verified:
            await self._emit("experience.verified", task_id, {"memory_id": str(mem.id)})

        # 2) the verified successful PROCEDURE → procedural memory (§6): scoped, evidence-bearing, NOT
        #    universal (§31). A failed approach is never written as a procedure (§9).
        if verified and record["procedure"]:
            await self._persist_procedure(task_id, record)
            # 3) a verified experience with a procedure establishes a scoped CAPABILITY candidate (§49) —
            #    "I can do this in THIS environment", evidence-backed, never a universal claim. Best-effort.
            with contextlib.suppress(Exception):
                from sali.learning.capability import CapabilityStore

                record["capability_name"] = await self._capability_name(record)
                store = CapabilityStore(self._pool, self._publisher)
                await store.from_experience(record)
                # AND CREDIT THE USE. `record_use` had no caller anywhere, so `last_used`, `use_count`
                # and `capability_usage` stayed empty forever: a capability could be "verified" at
                # confidence 0.95 having never once been exercised, which is the difference between a
                # claim and a competence. A finished task IS the exercise, and this is where the evidence
                # exists — the objective, the outcome, and the task id that backs it.
                await store.record_use(
                    name=record["capability_name"], scope="environment",
                    scope_ref=record.get("scope_ref") or "local",
                    action=(record.get("objective") or "")[:200],
                    result=(record.get("result") or record.get("headline") or "")[:400],
                    success=True, verified=verified, task_id=task_id)
                # The gap opened when this task was planned is closed by evidence, and this is also
                # where it finally gets its real name — the same name from _capability_name, so the
                # acquisition and the capability it produced agree.
                from sali.learning.capability_acquisition import CapabilityAcquisitionStore

                acq = CapabilityAcquisitionStore(self._pool, self._publisher)
                live = await acq.for_task(task_id)
                if live is not None:
                    await acq.acquired(UUID(str(live["id"])),
                                       capability_name=record["capability_name"])
        if record["lesson"]:
            await self._emit("experience.lesson_extracted", task_id,
                             {"memory_id": str(mem.id), "lesson": record["lesson"][:160]})
        await self._emit("memory.consolidated", task_id, {"memory_id": str(mem.id)})
        return record

    _NAME_PROMPT = (
        "A task just finished successfully. Name the reusable SKILL it demonstrates — not this task.\n\n"
        "Objective: {objective}\nSteps that worked: {steps}\n\n"
        "Answer with a short lowercase skill name, 2-5 words, no punctuation, no specifics: no file "
        "paths, no folder names, no project names, no counts. It must be the same name the next time a "
        "task of this kind succeeds, so that the skill is recognised instead of learned again.\n"
        "Good: write a python archive script / build a static tailwind site / configure an nginx vhost\n"
        "Bad: write a python backup script for the Photos folder into ~/Pictures/archive\n"
        "Return only the name."
    )

    async def _capability_name(self, record: dict[str, Any]) -> str:
        """The reusable name for the skill this task demonstrated.

        THIS IS THE ANTI-RELEARN HINGE. `from_experience` used to name the capability after the raw
        objective, so every task minted a unique string and nothing could ever match a later request:
        one row read "Write a Python backup script that compresses the Photos folder into a timestamped
        zip archive at ~/Pictures/archive/." — an objective, not a skill.

        I tried to derive the name mechanically first, because it would have been free. It does not work:
        normalising three real, semantically identical objectives produced three different keys
        (`write:compresses-photos-python`, `write:archives-dated-documents`,
        `write:photos-python-script`) because word choice decided identity. Naming has to be done by
        something that understands the sentence.

        So: ONE inference, once per completed task, on the background path after Almir already has his
        reply — never per turn. If there is no provider, or the call fails, or it answers with something
        unusable, the objective is used exactly as before, so this can only improve on the old behaviour.
        """
        objective = (record.get("objective") or "").strip()
        if self._provider is None or not objective:
            return objective[:120]
        try:
            from sali.provider.base import ChatMessage

            steps = record.get("procedure") or []
            res = await self._provider.chat([ChatMessage(
                role="user",
                content=self._NAME_PROMPT.format(
                    objective=objective[:400],
                    steps=", ".join(str(x) for x in steps[:8])[:400] or "(none recorded)"),
            )])
            name = " ".join((res.content or "").strip().lower().split())
            name = name.strip(" .\"'`\n")
            # Guard rails: a name that is too long, or that still carries the task's specifics, is worse
            # than useless — it looks reusable and never matches. Reject and fall back.
            if not (2 <= len(name.split()) <= 6):
                return objective[:120]
            if any(ch in name for ch in "/~\\") or any(c.isdigit() for c in name):
                return objective[:120]
            return name[:120]
        except Exception:  # noqa: BLE001 - naming is an improvement, never a requirement
            return objective[:120]

    async def _persist_procedure(self, task_id: UUID, record: dict[str, Any]) -> None:
        steps = record["procedure"]
        sig = hashlib.sha256(
            ("|".join(steps) + "|" + (record["scope_ref"] or "")).encode()).hexdigest()[:16]
        content = (f"Procedure for '{record['objective'][:80]}': " + " → ".join(steps)
                   + f" (verified in {record['scope_ref'] or 'this environment'})")
        with contextlib.suppress(Exception):
            async with self._pool.acquire() as conn:
                await memory_writer.remember(
                    conn, layer=MemoryLayer.PROCEDURAL, content=content,
                    source=MemorySource.PROCEDURE_EXECUTION, functional=True,
                    claim_key=f"procedure:exp:{sig}", importance=0.7, source_ref=task_id,
                    scope=record["scope_ref"] or "global",
                    structured={"kind": "procedure", "steps": steps, "objective": record["objective"],
                                "scope": record["scope"], "scope_ref": record["scope_ref"],
                                "known_failure_modes": [f["tool"] for f in record["failures"]][:5],
                                "evidence_state": EvidenceState.VERIFIED.value, "certainty": "verified",
                                "source_task": str(task_id)})

    # ── bounded retrieval of relevant PAST EXPERIENCES for a new task (§32/§49) ──────────────────────
    async def relevant_experiences(
        self, *, objective: str = "", scope_ref: str | None = None, limit: int = 5,
    ) -> list[dict[str, Any]]:
        """The past experiences most relevant to a new objective — bounded, keyword+evidence+recency
        ranked, contradicted/retired ones excluded (only current memories). Deterministic so it's
        testable offline; the semantic (pgvector) retriever is available via MemoryService for the
        richer path. Never dumps the whole store into context (§16/§42)."""
        keys = _keywords(objective)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, content, structured, importance, confidence, last_verified, scope "
                "FROM memory WHERE valid_until IS NULL AND structured->>'kind'='experience' "
                "ORDER BY last_verified DESC LIMIT 200")
        scored: list[tuple[float, dict[str, Any]]] = []
        for r in rows:
            st = r["structured"] or {}
            overlap = len(_keywords(r["content"] + " " + (st.get("objective") or "")) & keys)
            if overlap == 0 and not (scope_ref and r["scope"] == scope_ref):
                continue  # no relevance signal → excluded (§16 irrelevant memory is not injected)
            score = float(overlap) + float(r["confidence"] or 0) + float(r["importance"] or 0)
            if st.get("evidence_state") == EvidenceState.VERIFIED.value:
                score += 1.0
            if scope_ref and r["scope"] == scope_ref:
                score += 1.5
            scored.append((score, {"id": str(r["id"]), "content": r["content"],
                                   "objective": st.get("objective"), "procedure": st.get("procedure"),
                                   "failures": st.get("failures", []),
                                   "evidence_state": st.get("evidence_state"), "scope": r["scope"]}))
        scored.sort(key=lambda t: t[0], reverse=True)
        chosen = [rec for _, rec in scored[:limit]]
        if self._publisher is not None and chosen:
            with contextlib.suppress(Exception):
                await self._publisher.emit(event_type="memory.retrieved", subject_type="memory",
                                           origin="memory", data={"count": len(chosen), "kind": "experience"})
        return chosen

    def render(self, experiences: list[dict[str, Any]], *, per_item_chars: int = 220,
               max_items: int = 3) -> str:
        """A bounded block of relevant past experiences for the task context (§32/§34) — lowest
        priority, guidance only; the current task state always wins."""
        if not experiences:
            return ""
        parts = ["PAST EXPERIENCE (what you personally did before on similar problems — verify before "
                 "relying; it never overrides the current task, reviewer, or safety):"]
        for e in experiences[:max_items]:
            body = (e.get("content") or "").strip()
            if len(body) > per_item_chars:
                body = body[:per_item_chars].rstrip() + " …"
            parts.append(f"- [{e.get('evidence_state') or 'experience'}] {body}")
        return "\n".join(parts)

    # ── "why do I believe this?" — provenance (§11) ─────────────────────────────────────────────────
    async def provenance(self, memory_id: UUID) -> dict[str, Any] | None:
        """Reconstruct why Sali believes a memory: its source, the task/run it came from, and the
        recorded evidence — without inventing an explanation."""
        async with self._pool.acquire() as conn:
            mem = await conn.fetchrow(
                "SELECT id, content, layer, source, source_ref, structured, confidence, "
                "  evidence_count, valid_from, valid_until FROM memory WHERE id=$1", memory_id)
            if mem is None:
                return None
            evidence = await conn.fetch(
                "SELECT source, source_ref, confidence, note, observed_at FROM memory_evidence "
                "WHERE memory_id=$1 ORDER BY observed_at", memory_id)
        st = mem["structured"] or {}
        return {
            "memory_id": str(mem["id"]), "content": mem["content"], "layer": mem["layer"],
            "source": mem["source"], "confidence": mem["confidence"],
            "evidence_count": mem["evidence_count"],
            "task_id": st.get("task_id"), "run_id": st.get("run_id"),
            "evidence_state": st.get("evidence_state"), "verification": st.get("verification"),
            "research": st.get("research", []), "decisions": st.get("decisions", []),
            "evidence": [{"source": e["source"], "confidence": e["confidence"], "note": e["note"]}
                         for e in evidence],
        }

    # ── observability (§40) ─────────────────────────────────────────────────────────────────────────
    async def counts(self) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            by_layer = {r["layer"]: int(r["n"]) for r in await conn.fetch(
                "SELECT layer, count(*) AS n FROM memory WHERE valid_until IS NULL GROUP BY layer")}
            experiences = int(await conn.fetchval(
                "SELECT count(*) FROM memory WHERE valid_until IS NULL "
                "  AND structured->>'kind'='experience'") or 0)
            conflicts = int(await conn.fetchval(
                "SELECT count(*) FROM contradiction WHERE subject_type='memory' AND status='open'") or 0)
        return {"total": sum(by_layer.values()), "by_layer": by_layer,
                "experiences": experiences, "open_conflicts": conflicts}

    async def recent(self, *, limit: int = 10) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, content, structured, last_verified FROM memory "
                "WHERE valid_until IS NULL AND structured->>'kind'='experience' "
                "ORDER BY last_verified DESC LIMIT $1", limit)
        return [{"id": str(r["id"]), "content": r["content"],
                 "evidence_state": (r["structured"] or {}).get("evidence_state"),
                 "objective": (r["structured"] or {}).get("objective")} for r in rows]

    async def conflicts(self, *, limit: int = 20) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, old_id, new_id, old_source, new_source, resolution, status, created_at "
                "FROM contradiction WHERE subject_type='memory' ORDER BY created_at DESC LIMIT $1", limit)
        return [dict(r) for r in rows]

    async def _emit(self, event_type: str, task_id: UUID, data: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        with contextlib.suppress(Exception):
            await self._publisher.emit(event_type=event_type, task_id=task_id, subject_type="memory",
                                       origin="memory", data=data)


# ── deterministic distillation (no model) ───────────────────────────────────────────────────────────

_STOP = frozenset(("the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with", "is", "are",
                   "build", "create", "make", "app", "application", "task", "add", "using", "use"))


def _keywords(text: str | None) -> set[str]:
    import re
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) >= 3 and w not in _STOP}


def _build_record(
    task: Any, execs: list[Any], research: list[Any], decisions: list[Any], *,
    lesson: str | None, verified: bool,
) -> dict[str, Any]:
    """Distil the durable task graph into a compact, provenance-rich experience record. Pure."""
    failures: list[dict[str, Any]] = []
    successes: list[dict[str, Any]] = []
    procedure: list[str] = []
    for e in execs:
        ok = e["status"] in ("completed", "verified_success")
        failed = e["status"] in ("failed", "verified_failure")
        if failed:
            failures.append({"tool": e["tool_name"], "step": e["step_seq"],
                             "error": (e["error"] or "")[:200] or None})
        elif ok:
            successes.append({"tool": e["tool_name"], "step": e["step_seq"],
                              "result": (e["result_summary"] or "")[:120] or None})
            if not procedure or procedure[-1] != e["tool_name"]:
                procedure.append(e["tool_name"])
    fail_summary = "; ".join(f"{f['tool']} failed" + (f" ({f['error'][:60]})" if f["error"] else "")
                             for f in failures[:3])
    succ_summary = (lesson or (successes[-1]["result"] if successes and successes[-1]["result"] else None)
                    or (" → ".join(procedure) if procedure else "completed"))
    headline = f"Experience — {task['objective']}: {succ_summary}"
    if fail_summary:
        headline += f". First attempts that failed: {fail_summary}"
    if verified:
        headline += ". Verified by reviewer PASS."
    # scope is derived: an experience with a durable workspace is project-scoped (reusable in that
    # project/environment); otherwise it is task-scoped. Generalization to 'global' is left to
    # consolidation with sufficient evidence (§31) — one success is never a universal rule.
    scope = "project" if task["workspace_root"] else "task"
    return {
        "kind": "experience", "task_id": str(task["id"]), "objective": task["objective"],
        "scope": scope, "scope_ref": task["workspace_root"],
        "evidence_state": EvidenceState.VERIFIED.value if verified else EvidenceState.ATTEMPTED.value,
        "verification": "reviewer_pass" if verified else None,
        "attempts": len(execs), "failures": failures[:_MAX_FAILURES], "successes": successes[:_MAX_ITEMS],
        "procedure": procedure, "research": [{"query": r["query"], "source": r["source"]} for r in research],
        "decisions": [{"decision": d["decision"], "reason": d["reason"]} for d in decisions],
        "lesson": lesson, "certainty": "verified" if verified else "attempted", "headline": headline,
    }
