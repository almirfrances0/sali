"""Lifetime memory & experience (Prompt: lifetime memory §53).

The invariant under test: Sali may forget disposable conversational context, but he must not lose
durable experience. A PASSed task becomes a distilled, provenance-rich experience that survives
workspace deletion and restart; failures are kept as negative knowledge and never become a verified
capability; knowledge carries evidence + provenance + temporal validity; secrets never land in memory.
The substrate is the existing `memory` table (reused, not duplicated) — these tests prove the lifetime
layer built on top of it honours every rule.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from sali.core.enums import MemoryLayer, MemorySource
from sali.events.publisher import EventPublisher
from sali.learning.experience import EvidenceState, ExperienceStore
from sali.memory import writer
from sali.tasks.reviewer import TaskReviewer
from sali.tasks.store import TaskStore

pytestmark = pytest.mark.db


def _wired_store(pool: Any) -> tuple[TaskStore, EventPublisher, ExperienceStore]:
    """A TaskStore wired exactly as the runtime wires it: reviewer gate + experience-extraction hook."""
    pub = EventPublisher(pool)
    store = TaskStore(pool, pub)
    store._reviewer = TaskReviewer(pool, pub)
    exp = ExperienceStore(pool, pub)
    store._experience_hook = exp.extract_and_persist
    return store, pub, exp


async def _complete_all_steps(pool: Any, task_id: UUID) -> None:
    async with pool.acquire() as c:
        await c.execute("UPDATE task_step SET status='done', verified=true WHERE task_id=$1", task_id)


async def _pass_task_with_experience(pool: Any, objective: str, *, workspace: str | None = None) -> UUID:
    """Create a task, record a FAILED attempt then a SUCCESSFUL one with distinct tools, and finish it
    through the reviewer gate — firing the experience hook before cleanup. Returns the (now-deleted) id."""
    store, _pub, _exp = _wired_store(pool)
    t = await store.create(objective, ["do the thing"])
    await store.activate(t.id)
    await store.bind_workspace(t.id, objective=objective, explicit=None,
                               sali_works_root=workspace or "/home/almir/Desktop/sali-works")
    e1 = await store.record_execution(t.id, 1, "approach_a", attempt=1)
    await store.complete_execution(e1, status="failed", error="approach_a failed: port already in use")
    e2 = await store.record_execution(t.id, 1, "approach_b", attempt=2)
    await store.complete_execution(e2, status="completed", result_summary="approach_b worked on port 8080")
    await _complete_all_steps(pool, t.id)
    assert await store.finish(t.id, status="done") is None
    return t.id


async def _mem(pool: Any, memory_id: UUID) -> Any:
    async with pool.acquire() as c:
        return await c.fetchrow("SELECT * FROM memory WHERE id=$1", memory_id)


# ── episodic: significant experience persisted, survives cleanup + restart (§4/§18/§19) ─────────────

async def test_passed_task_creates_durable_experience_that_survives_cleanup(live_pool: Any) -> None:
    store, _pub, exp = _wired_store(live_pool)
    tid = await _pass_task_with_experience(live_pool, "Deploy the Laravel app to staging")
    # the task + its workspace/graph are cleaned, but the experience remains (workspace disposable §19)
    assert await store.get(tid) is None
    recent = await exp.recent()
    assert recent and "Deploy the Laravel app to staging" in recent[0]["content"]
    assert recent[0]["evidence_state"] == EvidenceState.VERIFIED.value


async def test_experience_survives_restart(live_pool: Any) -> None:
    await _pass_task_with_experience(live_pool, "Configure nginx reverse proxy")
    # a brand-new ExperienceStore (simulated process restart) reconstructs it from PostgreSQL alone
    recent = await ExperienceStore(live_pool).recent()
    assert any("nginx reverse proxy" in r["content"] for r in recent)


# ── negative knowledge kept; failed approach never a verified capability (§9/§29) ───────────────────

async def test_failure_is_negative_knowledge_and_never_a_verified_procedure(live_pool: Any) -> None:
    tid = await _pass_task_with_experience(live_pool, "Start the dev server")
    exp = ExperienceStore(live_pool)
    rec = (await exp.recent())[0]
    st = (await _mem(live_pool, UUID(rec["id"])))["structured"]
    # the failed approach is recorded as negative knowledge …
    assert any(f["tool"] == "approach_a" for f in st["failures"])
    assert any("port already in use" in (f.get("error") or "") for f in st["failures"])
    # … and the ONLY verified procedure is the successful approach — the failure never became a capability
    assert st["procedure"] == ["approach_b"]
    async with live_pool.acquire() as c:
        proc = await c.fetchrow(
            "SELECT structured FROM memory WHERE layer='procedural' AND valid_until IS NULL "
            "  AND structured->>'source_task'=$1", str(tid))
    assert proc is not None and proc["structured"]["steps"] == ["approach_b"]
    assert "approach_a" not in proc["structured"]["steps"]


# ── provenance + evidence preserved (§8/§10/§11) ────────────────────────────────────────────────────

async def test_experience_carries_provenance_and_evidence(live_pool: Any) -> None:
    tid = await _pass_task_with_experience(live_pool, "Run the database migrations")
    exp = ExperienceStore(live_pool)
    rec = (await exp.recent())[0]
    prov = await exp.provenance(UUID(rec["id"]))
    assert prov is not None
    assert prov["task_id"] == str(tid)                 # traceable to the task it came from (§11)
    assert prov["evidence_state"] == EvidenceState.VERIFIED.value
    assert prov["verification"] == "reviewer_pass"     # reviewer PASS is the verification (§30)
    assert prov["source"] == MemorySource.SYSTEM_OBSERVATION.value  # a lived experience, not a claim (§8)
    assert prov["evidence"]                            # at least one memory_evidence row


# ── verified procedure becomes procedural memory, scoped not universal (§6/§31) ──────────────────────

async def test_verified_procedure_becomes_scoped_procedural_memory(live_pool: Any) -> None:
    await _pass_task_with_experience(live_pool, "Provision the cache layer", workspace="/home/almir/Desktop/sali-works")
    async with live_pool.acquire() as c:
        proc = await c.fetchrow(
            "SELECT content, structured, scope FROM memory WHERE layer='procedural' "
            "  AND valid_until IS NULL AND structured->>'objective'='Provision the cache layer'")
    assert proc is not None
    st = proc["structured"]
    assert st["evidence_state"] == EvidenceState.VERIFIED.value
    assert st["scope_ref"] and st["scope_ref"] != "global"   # scoped to the environment, not universal (§31)


# ── consolidation: duplicate experiences merge, not multiply (§17/§46) ───────────────────────────────

async def test_duplicate_experience_extraction_consolidates(live_pool: Any) -> None:
    # a task that stays around (not finished) so we can extract twice; second extraction must MERGE
    pub = EventPublisher(live_pool)
    store = TaskStore(live_pool, pub)
    exp = ExperienceStore(live_pool, pub)
    t = await store.create("Index the documents", ["index"])
    await store.activate(t.id)
    e = await store.record_execution(t.id, 1, "indexer", attempt=1)
    await store.complete_execution(e, status="completed", result_summary="indexed 100 docs")
    r1 = await exp.extract_and_persist(t.id)
    r2 = await exp.extract_and_persist(t.id)
    assert r1 is not None and r2 is not None and r1["memory_id"] == r2["memory_id"]  # merged (§46)
    async with live_pool.acquire() as c:
        n = await c.fetchval(
            "SELECT count(*) FROM memory WHERE valid_until IS NULL AND structured->>'kind'='experience' "
            "  AND structured->>'task_id'=$1", str(t.id))
    assert n == 1  # one experience, corroborated — not duplicated


# ── retrieval: relevant in, irrelevant out, bounded, evidence + temporal respected (§15/§16/§48) ─────

async def _seed_experience(pool: Any, *, content: str, objective: str,
                           evidence_state: str = "verified", scope: str = "global") -> UUID:
    async with pool.acquire() as c:
        m = await writer.remember(
            c, layer=MemoryLayer.EPISODIC, content=content, source=MemorySource.SYSTEM_OBSERVATION,
            importance=0.7, scope=scope,
            structured={"kind": "experience", "objective": objective, "evidence_state": evidence_state,
                        "failures": [], "procedure": []})
    return m.id


async def test_relevant_experience_retrieved_irrelevant_excluded_and_bounded(live_pool: Any) -> None:
    exp = ExperienceStore(live_pool)
    await _seed_experience(live_pool, content="Experience — deploy a Laravel project: used Breeze, verified",
                           objective="deploy a Laravel project")
    await _seed_experience(live_pool, content="Experience — prune Docker images to reclaim disk",
                           objective="prune Docker images")
    for i in range(8):
        await _seed_experience(live_pool, content=f"Experience — Laravel migration run {i}",
                               objective=f"Laravel migration {i}")
    hits = await exp.relevant_experiences(objective="build and deploy a Laravel application", limit=3)
    joined = " ".join(h["content"] for h in hits)
    assert "Laravel" in joined and "Docker" not in joined   # relevant in, irrelevant out (§16)
    assert len(hits) <= 3                                    # bounded (§15/§42)


async def test_retrieval_respects_evidence_strength(live_pool: Any) -> None:
    exp = ExperienceStore(live_pool)
    await _seed_experience(live_pool, content="Experience — kubernetes rollout attempted only",
                           objective="kubernetes rollout", evidence_state="attempted")
    verified = await _seed_experience(
        live_pool, content="Experience — kubernetes rollout verified working",
        objective="kubernetes rollout", evidence_state="verified")
    hits = await exp.relevant_experiences(objective="kubernetes rollout", limit=2)
    assert hits[0]["id"] == str(verified)   # the verified experience outranks the merely-attempted one


async def test_retrieval_respects_temporal_validity(live_pool: Any) -> None:
    exp = ExperienceStore(live_pool)
    mid = await _seed_experience(live_pool, content="Experience — old redis setup that no longer applies",
                                 objective="redis setup")
    assert await exp.relevant_experiences(objective="redis setup")     # retrievable while current
    async with live_pool.acquire() as c:
        await writer.forget(c, mid, reason="superseded setup")          # retire it (close the interval)
    assert not await exp.relevant_experiences(objective="redis setup")  # excluded once retired (§13/§36)


# ── substrate invariants the directive requires (reused memory writer) ──────────────────────────────

async def test_contradictory_knowledge_does_not_silently_overwrite(live_pool: Any) -> None:
    async with live_pool.acquire() as c:
        old = await writer.remember(
            c, layer=MemoryLayer.SEMANTIC, content="Package X supports feature Y",
            source=MemorySource.EXTERNAL_SOURCE, functional=True, claim_key="pkgx_featureY")
        new = await writer.remember(
            c, layer=MemoryLayer.SEMANTIC, content="Package X removed feature Y in v3",
            source=MemorySource.SYSTEM_OBSERVATION, functional=True, claim_key="pkgx_featureY")
    assert old.id != new.id
    async with live_pool.acquire() as c:
        old_row = await c.fetchrow("SELECT valid_until, superseded_by FROM memory WHERE id=$1", old.id)
        cx = await c.fetchval("SELECT count(*) FROM contradiction WHERE subject_type='memory' AND old_id=$1", old.id)
    assert old_row["valid_until"] is not None and old_row["superseded_by"] == new.id  # superseded, not deleted
    assert cx == 1                                                                    # contradiction recorded (§47)


async def test_explicit_correction_supersedes_inference_and_source_is_preserved(live_pool: Any) -> None:
    async with live_pool.acquire() as c:
        inferred = await writer.remember(
            c, layer=MemoryLayer.PREFERENCE, content="Almir may prefer tabs",
            source=MemorySource.INFERENCE, functional=True, claim_key="pref_indent")
        explicit = await writer.remember(
            c, layer=MemoryLayer.PREFERENCE, content="Almir uses spaces",
            source=MemorySource.USER_EXPLICIT, functional=True, claim_key="pref_indent")
    async with live_pool.acquire() as c:
        cur = await c.fetchrow(
            "SELECT id, source FROM memory WHERE claim_key='pref_indent' AND valid_until IS NULL")
        old = await c.fetchrow("SELECT source FROM memory WHERE id=$1", inferred.id)
    assert cur["id"] == explicit.id and cur["source"] == MemorySource.USER_EXPLICIT.value  # explicit wins
    assert old["source"] == MemorySource.INFERENCE.value   # the inference stays distinguishable in history


async def test_secrets_are_not_persisted_into_memory(live_pool: Any) -> None:
    async with live_pool.acquire() as c:
        mem = await writer.remember(
            c, layer=MemoryLayer.SEMANTIC, content="the db password=hunter2 and it works",
            source=MemorySource.USER_EXPLICIT)
    assert "hunter2" not in mem.content and "[redacted]" in mem.content   # redacted at the write boundary (§38)


# ── observability (§40) ─────────────────────────────────────────────────────────────────────────────

async def test_memory_counts_expose_experiences_and_conflicts(live_pool: Any) -> None:
    await _pass_task_with_experience(live_pool, "Set up CI pipeline")
    counts = await ExperienceStore(live_pool).counts()
    assert counts["experiences"] >= 1 and counts["total"] >= 1 and "episodic" in counts["by_layer"]
