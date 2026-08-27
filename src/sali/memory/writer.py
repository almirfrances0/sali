"""The memory write path.

Two entry points with a hard wall between them — the *structural* guarantee that working
memory cannot silently become permanent (rules 3, 4):

* ``observe()`` writes only to non-persistent ``stm_observation`` (+ the durable event log).
* ``remember()`` is the only INSERT into ``memory``, refuses ``layer='working'``, and:
  - restatement of the same content → corroborates (raises confidence);
  - a *functional* claim (``functional=True`` + ``claim_key``) with a different value →
    contradiction resolution by evidence priority (close-and-supersede; the memory analogue
    of the graph's ``set_fact``), never overwriting history (rules 6/8).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sali.core.enums import (
    FreshnessPolicy,
    MemoryLayer,
    MemorySource,
    compare_sources,
    contradiction_lifecycle,
    source_priority,
)
from sali.core.errors import SaliError
from sali.memory.confidence import apply_evidence, bump_reliability, initial_confidence
from sali.memory.models import Memory, row_to_memory


async def observe(
    conn: Any,
    *,
    kind: str,
    content: str,
    source: MemorySource,
    session_id: UUID | None = None,
    run_id: UUID | None = None,
    confidence: float = 0.5,
    salience: float = 0.5,
    source_ref: UUID | None = None,
    structured: dict[str, Any] | None = None,
) -> UUID:
    """Record a transient working-memory observation. Never reaches ``memory``."""
    row = await conn.fetchrow(
        "INSERT INTO stm_observation "
        "  (session_id, run_id, kind, content, source, source_ref, confidence, salience, structured) "
        "VALUES ($1,$2,$3,$4,$5::memory_source,$6,$7,$8,$9) RETURNING id",
        session_id, run_id, kind, content, source.value, source_ref, confidence, salience,
        structured or {},
    )
    await conn.execute(
        "INSERT INTO event (event_type, payload) VALUES ('memory.observed', $1)",
        {"kind": kind, "source": source.value},
    )
    return row["id"]  # type: ignore[no-any-return]


async def remember(
    conn: Any,
    *,
    layer: MemoryLayer,
    content: str,
    source: MemorySource,
    obs_conf: float = 1.0,
    freshness: FreshnessPolicy | None = None,
    importance: float | None = None,
    claim_key: str | None = None,
    functional: bool = False,
    source_ref: UUID | None = None,
    structured: dict[str, Any] | None = None,
    needs_grounding: bool = False,
    note: str | None = None,
    scope: str = "global",
    at: datetime | None = None,
) -> Memory:
    """Persist a fact. Corroborates on restatement; resolves functional-claim conflicts."""
    if layer is MemoryLayer.WORKING:
        raise SaliError("working memory is not persistent; use observe()")

    policy = await conn.fetchrow(
        "SELECT default_freshness, base_importance FROM layer_policy WHERE layer=$1::memory_layer",
        layer.value,
    )
    fresh = freshness or (
        FreshnessPolicy(policy["default_freshness"]) if policy else FreshnessPolicy.SLOW
    )
    imp = importance if importance is not None else (policy["base_importance"] if policy else 0.5)

    if functional and claim_key is not None:
        return await _resolve_claim(
            conn, layer=layer, content=content, source=source, obs_conf=obs_conf, freshness=fresh,
            importance=imp, claim_key=claim_key, source_ref=source_ref, structured=structured,
            needs_grounding=needs_grounding, note=note, scope=scope, at=at,
        )

    dedup = (
        "SELECT * FROM memory WHERE layer=$1::memory_layer AND content_hash=digest($2,'sha256') "
        "  AND valid_until IS NULL AND superseded_by IS NULL FOR UPDATE"
    )
    async with conn.transaction():
        existing = await conn.fetchrow(dedup, layer.value, content)
        if existing is not None:
            # Locked read-modify-write: no lost confidence/evidence under concurrency.
            return await _corroborate(conn, existing, source, obs_conf, note, source_ref)
        try:
            async with conn.transaction():  # savepoint guarding the ux_memory_current race
                return await _insert_memory(
                    conn, layer=layer, content=content, source=source, obs_conf=obs_conf,
                    freshness=fresh, importance=imp, claim_key=claim_key, functional=functional,
                    source_ref=source_ref, structured=structured, needs_grounding=needs_grounding,
                    note=note, scope=scope, valid_from=at,
                )
        except Exception as exc:  # noqa: BLE001 - narrowed to unique-violation immediately below
            if getattr(exc, "sqlstate", None) != "23505":  # not unique_violation → real error
                raise
            # A concurrent writer inserted the same content first; corroborate it instead.
            existing = await conn.fetchrow(dedup, layer.value, content)
            if existing is None:
                raise
            return await _corroborate(conn, existing, source, obs_conf, note, source_ref)


async def _resolve_claim(
    conn: Any,
    *,
    layer: MemoryLayer,
    content: str,
    source: MemorySource,
    obs_conf: float,
    freshness: FreshnessPolicy,
    importance: float,
    claim_key: str,
    source_ref: UUID | None,
    structured: dict[str, Any] | None,
    needs_grounding: bool,
    note: str | None,
    scope: str,
    at: datetime | None,
) -> Memory:
    async with conn.transaction():
        # Advisory lock on the claim serializes writers even when the slot is empty
        # (FOR UPDATE alone locks nothing on an empty result) — fix M1.
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"mclaim:{claim_key}"
        )
        effective_at = at if at is not None else await conn.fetchval("SELECT now()")
        current = await conn.fetchrow(
            "SELECT * FROM memory WHERE claim_key=$1 AND functional "
            "  AND valid_until IS NULL AND superseded_by IS NULL FOR UPDATE",
            claim_key,
        )
        insert: dict[str, Any] = {
            "layer": layer, "content": content, "source": source, "obs_conf": obs_conf,
            "freshness": freshness, "importance": importance, "claim_key": claim_key,
            "functional": True, "source_ref": source_ref, "structured": structured,
            "needs_grounding": needs_grounding, "note": note, "scope": scope,
        }
        if current is None:
            return await _insert_memory(conn, **insert, valid_from=effective_at)
        if current["content"] == content:
            return await _corroborate(conn, current, source, obs_conf, note, source_ref)

        decision = compare_sources(source, MemorySource(current["source"]))
        if decision in ("new", "tie"):
            # Close the old row FIRST so the partial unique index on claim_key sees only one
            # current value at INSERT time; then link the supersession.
            await conn.execute(
                "UPDATE memory SET valid_until=$2, updated_at=now() WHERE id=$1",
                current["id"], effective_at,
            )
            new_mem = await _insert_memory(conn, **insert, valid_from=effective_at)
            await conn.execute(
                "UPDATE memory SET superseded_by=$2 WHERE id=$1", current["id"], new_mem.id
            )
            await _record_memory_contradiction(
                conn, current["id"], new_mem.id, MemorySource(current["source"]), source,
                "new_wins" if decision == "new" else "recency_tiebreak",
            )
            return new_mem

        # Stronger old source wins: record the new claim as closed dissent, keep old current.
        dissent = await _insert_memory(
            conn, **insert, valid_from=effective_at, valid_until=effective_at
        )
        await _record_memory_contradiction(
            conn, current["id"], dissent.id, MemorySource(current["source"]), source, "old_wins"
        )
        return row_to_memory(current)


async def _insert_memory(
    conn: Any,
    *,
    layer: MemoryLayer,
    content: str,
    source: MemorySource,
    obs_conf: float,
    freshness: FreshnessPolicy,
    importance: float,
    claim_key: str | None,
    functional: bool,
    source_ref: UUID | None,
    structured: dict[str, Any] | None,
    needs_grounding: bool,
    note: str | None,
    scope: str = "global",
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
) -> Memory:
    conf = initial_confidence(source, obs_conf)
    rel = bump_reliability(0.0, source)
    row = await conn.fetchrow(
        "INSERT INTO memory "
        "  (layer, content, source, source_ref, structured, claim_key, functional, confidence, "
        "   importance, reliability, evidence_count, freshness, needs_grounding, scope, "
        "   valid_from, valid_until) "
        "VALUES ($1::memory_layer,$2,$3::memory_source,$4,$5,$6,$7,$8,$9,$10,1,$11::freshness_policy,"
        "        $12, $13, COALESCE($14::timestamptz, now()), $15) RETURNING *",
        layer.value, content, source.value, source_ref, structured or {}, claim_key, functional,
        conf, importance, rel, freshness.value, needs_grounding, scope, valid_from, valid_until,
    )
    await conn.execute(
        "INSERT INTO memory_evidence (memory_id, source, source_ref, confidence, note) "
        "VALUES ($1,$2::memory_source,$3,$4,$5)",
        row["id"], source.value, source_ref, obs_conf, note,
    )
    await conn.execute(
        "INSERT INTO event (event_type, subject_type, subject_id, payload) "
        "VALUES ('memory.created','memory',$1,$2)",
        row["id"], {"layer": layer.value, "source": source.value, "functional": functional},
    )
    return row_to_memory(row)


async def _corroborate(
    conn: Any,
    existing: Any,
    source: MemorySource,
    obs_conf: float,
    note: str | None,
    source_ref: UUID | None,
) -> Memory:
    # Caller holds the transaction and has locked `existing` FOR UPDATE, so recomputing from
    # this snapshot is safe against concurrent corroborations.
    new_conf = apply_evidence(existing["confidence"], source, obs_conf)
    new_rel = bump_reliability(existing["reliability"], source)
    row = await conn.fetchrow(
        "UPDATE memory SET confidence=$1, reliability=$2, evidence_count=evidence_count+1, "
        "  last_seen=now(), last_verified=now(), updated_at=now() WHERE id=$3 RETURNING *",
        new_conf, new_rel, existing["id"],
    )
    await conn.execute(
        "INSERT INTO memory_evidence (memory_id, source, source_ref, confidence, note) "
        "VALUES ($1,$2::memory_source,$3,$4,$5)",
        existing["id"], source.value, source_ref, obs_conf, note,
    )
    await conn.execute(
        "INSERT INTO event (event_type, subject_type, subject_id, payload) "
        "VALUES ('memory.corroborated','memory',$1,$2)",
        existing["id"], {"source": source.value},
    )
    return row_to_memory(row)


async def _record_memory_contradiction(
    conn: Any,
    old_id: UUID,
    new_id: UUID,
    old_source: MemorySource,
    new_source: MemorySource,
    resolution: str,
) -> None:
    # Memory facts have no automatic re-observer, so we never leave one 'open' (it would never close).
    # But a fact settled by a live inspection is honestly labelled 'verification', not a priority guess.
    _, resolved_by = contradiction_lifecycle(old_source, new_source, verifiable=False)
    await conn.execute(
        "INSERT INTO contradiction (subject_type, old_id, new_id, status, old_source, new_source, "
        "  old_priority, new_priority, resolution, resolved_by, resolved_at) "
        "VALUES ('memory',$1,$2,'resolved',$3::memory_source,$4::memory_source,$5,$6,$7,$8,now())",
        old_id, new_id, old_source.value, new_source.value,
        source_priority(old_source), source_priority(new_source), resolution, resolved_by,
    )
    await conn.execute(
        "INSERT INTO event (event_type, subject_type, subject_id, payload) "
        "VALUES ('memory.contradiction','memory',$1,$2)",
        old_id, {"resolution": resolution, "new_id": str(new_id), "resolved_by": resolved_by},
    )


async def forget(conn: Any, memory_id: UUID, *, reason: str) -> bool:
    """Retire a memory from CURRENT knowledge by closing its validity interval — never a hard delete,
    so the history stays recoverable (§51). Returns True if it was current and is now retired."""
    result = await conn.execute(
        "UPDATE memory SET valid_until=now(), updated_at=now() "
        "WHERE id=$1 AND valid_until IS NULL AND superseded_by IS NULL",
        memory_id,
    )
    closed = bool(result.split()[-1] == "1")
    if closed:
        await conn.execute(
            "INSERT INTO event (event_type, subject_type, subject_id, payload) "
            "VALUES ('memory.forgotten','memory',$1,$2)",
            memory_id, {"reason": reason[:200]},
        )
    return closed


async def reground(conn: Any, memory_id: UUID, *, verified: bool, note: str | None = None) -> bool:
    """Re-ground a memory against reality (§40): a confirmed fact clears needs_grounding, refreshes
    last_verified and nudges confidence up; a contradicted one drops confidence and is re-flagged. An
    evidence row records the check either way. Returns True if a current memory was updated."""
    if verified:
        result = await conn.execute(
            "UPDATE memory SET last_verified=now(), needs_grounding=false, "
            "  confidence=least(1.0, confidence + 0.05), updated_at=now() "
            "WHERE id=$1 AND valid_until IS NULL",
            memory_id,
        )
    else:
        result = await conn.execute(
            "UPDATE memory SET confidence=greatest(0.1, confidence - 0.2), needs_grounding=true, "
            "  updated_at=now() WHERE id=$1 AND valid_until IS NULL",
            memory_id,
        )
    if result.split()[-1] != "1":
        return False
    await conn.execute(
        "INSERT INTO memory_evidence (memory_id, source, confidence, note) "
        "VALUES ($1,'system_observation'::memory_source,$2,$3)",
        memory_id, 0.9 if verified else 0.3,
        note or ("verified against reality" if verified else "contradicted by reality"),
    )
    return True
