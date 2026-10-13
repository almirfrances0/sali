"""Graph writes: nodes, many-valued relations, and single-valued (functional) facts.

A *functional* fact is a slot with one current value (``desktop -runs_model-> X``). Asserting
a new value for it is where temporal history and contradictions live: the current row is
locked ``FOR UPDATE`` (serializing concurrent writers — fix M1), then resolved by evidence
priority. A stronger new source closes-and-supersedes the old; a weaker one is kept as
recorded dissent (a closed interval that was never current); an equal-priority tie goes to
recency — but the outcome is *always* a single current value, recorded, never silently
picked (fix M3). Nothing is ever overwritten (rules 6/8).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sali.core.enums import (
    MemorySource,
    compare_sources,
    contradiction_lifecycle,
    is_observation,
    source_priority,
)
from sali.graph.models import ContradictionOutcome, Edge, Node, row_to_edge, row_to_node


async def ensure_node(
    conn: Any,
    *,
    node_type: str,
    name: str,
    canonical_key: str,
    source: MemorySource,
    confidence: float = 1.0,
    props: dict[str, Any] | None = None,
    memory_id: UUID | None = None,
    at: datetime | None = None,
) -> Node:
    """Create the node, or corroborate an existing one (identity is stable; state lives in
    the twin, so re-observation bumps last_verified and merges props)."""
    existing = await conn.fetchrow(
        "SELECT * FROM graph_node WHERE node_type=$1 AND canonical_key=$2 AND valid_until IS NULL",
        node_type, canonical_key,
    )
    if existing is not None:
        # Re-observation bumps last_seen (not last_verified, which is for explicit verify).
        # Props merge is additive — existing values win, so a weaker source can't clobber
        # an established attribute (the opposite mistake would undercut set_fact's guarantee).
        # A NEW source attesting the node raises confidence (§6/§48); a repeat just refreshes it.
        fresh_source = await _note_evidence(
            conn, node_id=existing["id"], source=source, confidence=confidence, source_ref=memory_id)
        new_conf = _corroborated(existing["confidence"], confidence) if fresh_source else existing["confidence"]
        row = await conn.fetchrow(
            "UPDATE graph_node SET last_seen=COALESCE($2::timestamptz, now()), "
            "  confidence=$3, props = $4 || props WHERE id=$1 RETURNING *",
            existing["id"], at, new_conf, props or {},
        )
        return row_to_node(row)
    row = await conn.fetchrow(
        "INSERT INTO graph_node (node_type, name, canonical_key, props, source, confidence, "
        "  valid_from, last_verified, last_seen, memory_id) "
        "VALUES ($1,$2,$3,$4,$5::memory_source,$6, COALESCE($7::timestamptz, now()), "
        "  COALESCE($7::timestamptz, now()), COALESCE($7::timestamptz, now()), $8) RETURNING *",
        node_type, name, canonical_key, props or {}, source.value, confidence, at, memory_id,
    )
    await _note_evidence(conn, node_id=row["id"], source=source, confidence=confidence, source_ref=memory_id)
    return row_to_node(row)


async def refresh_props(
    conn: Any, node_id: UUID, props: dict[str, Any], *, name: str | None = None
) -> None:
    """Update a node's current attributes (and display name) so re-observation reflects changes —
    a version bump, a resized disk, a renamed thing. Unlike ``ensure_node``'s additive merge
    (existing wins, to protect established identity), this is new-wins on the given keys and name:
    the twin owns this node's state and is the authority on it. Also bumps last_seen."""
    await conn.execute(
        "UPDATE graph_node SET props = props || $2, name = COALESCE($3, name), "
        "last_seen = now() WHERE id=$1",
        node_id, props, name,
    )


async def _note_evidence(
    conn: Any, *, source: MemorySource, confidence: float, source_ref: UUID | None,
    edge_id: UUID | None = None, node_id: UUID | None = None,
) -> bool:
    """Record that `source` attests this fact — once per (fact, source), so re-observation by the same
    source doesn't pile up rows. Returns True if this is a NEW source for the fact (§6/§48)."""
    col = "edge_id" if edge_id is not None else "node_id"
    ref = edge_id if edge_id is not None else node_id
    if await conn.fetchval(
        f"SELECT 1 FROM graph_evidence WHERE {col}=$1 AND source=$2::memory_source", ref, source.value):
        return False
    await conn.execute(
        f"INSERT INTO graph_evidence ({col}, source, source_ref, confidence) "
        "VALUES ($1,$2::memory_source,$3,$4)",
        ref, source.value, source_ref, confidence)
    return True


def _corroborated(current: float, incoming: float) -> float:
    """Noisy-OR corroboration (§48): an INDEPENDENT source agreeing closes part of the gap to certainty,
    weighted by that source's confidence. Bounded below 1 — corroboration strengthens, never proves."""
    return min(0.99, current + (1.0 - current) * incoming * 0.5)


async def relate(
    conn: Any,
    *,
    src_id: UUID,
    dst_id: UUID,
    rel_type: str,
    source: MemorySource,
    confidence: float = 1.0,
    props: dict[str, Any] | None = None,
    memory_id: UUID | None = None,
    at: datetime | None = None,
) -> Edge:
    """Assert a many-valued relationship (contains, depends_on…). Dedups-and-corroborates."""
    existing = await conn.fetchrow(
        "SELECT * FROM graph_edge WHERE src_id=$1 AND dst_id=$2 AND rel_type=$3 "
        "  AND valid_until IS NULL AND superseded_by IS NULL",
        src_id, dst_id, rel_type,
    )
    if existing is not None:
        # A new independent source corroborating raises confidence (§6/§48); a repeat just refreshes.
        fresh_source = await _note_evidence(
            conn, edge_id=existing["id"], source=source, confidence=confidence, source_ref=memory_id)
        new_conf = _corroborated(existing["confidence"], confidence) if fresh_source else existing["confidence"]
        row = await conn.fetchrow(
            "UPDATE graph_edge SET last_verified=COALESCE($2::timestamptz, now()), "
            "  confidence=$3 WHERE id=$1 RETURNING *",
            existing["id"], at, new_conf,
        )
        return row_to_edge(row)
    row = await _insert_edge(conn, src_id, dst_id, rel_type, props, source, confidence, memory_id, at)
    return row_to_edge(row)


async def set_fact(
    conn: Any,
    *,
    src_id: UUID,
    rel_type: str,
    dst_id: UUID,
    source: MemorySource,
    confidence: float = 1.0,
    props: dict[str, Any] | None = None,
    memory_id: UUID | None = None,
    at: datetime | None = None,
) -> tuple[Edge, ContradictionOutcome | None]:
    """Assert a single-valued (functional) fact, resolving any conflict by evidence priority."""
    async with conn.transaction():
        # A transaction-scoped advisory lock on the slot serializes writers even when the
        # slot is EMPTY (first assertion, or just-superseded) — a plain FOR UPDATE locks
        # nothing on an empty result, so two concurrent asserts could both create a current
        # value. The lock auto-releases on commit/rollback (fix M1, empty-slot case).
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"gedge:{src_id}:{rel_type}"
        )
        effective_at = at if at is not None else await conn.fetchval("SELECT now()")
        # A fresh live look at this slot settles any open contradiction about it — the "then verify"
        # step (§20/§25). Runs whether or not the observed value changed (it may just confirm).
        if is_observation(source):
            await _verify_open_by_observation(conn, src_id, rel_type, effective_at)
        current = await conn.fetch(
            "SELECT * FROM graph_edge WHERE src_id=$1 AND rel_type=$2 "
            "  AND valid_until IS NULL AND superseded_by IS NULL FOR UPDATE",
            src_id, rel_type,
        )
        same = next((e for e in current if e["dst_id"] == dst_id), None)
        if same is not None:
            fresh_source = await _note_evidence(
                conn, edge_id=same["id"], source=source, confidence=confidence, source_ref=memory_id)
            new_conf = _corroborated(same["confidence"], confidence) if fresh_source else same["confidence"]
            row = await conn.fetchrow(
                "UPDATE graph_edge SET last_verified=$2, confidence=$3 WHERE id=$1 RETURNING *",
                same["id"], effective_at, new_conf,
            )
            return row_to_edge(row), None

        others = [e for e in current if e["dst_id"] != dst_id]
        if not others:
            row = await _insert_edge(
                conn, src_id, dst_id, rel_type, props, source, confidence, memory_id, effective_at
            )
            return row_to_edge(row), None

        old = others[0]
        decision = compare_sources(source, MemorySource(old["source"]))
        if decision in ("new", "tie"):
            new_row = await _insert_edge(
                conn, src_id, dst_id, rel_type, props, source, confidence, memory_id, effective_at
            )
            for other in others:  # close every conflicting current value
                await conn.execute(
                    "UPDATE graph_edge SET valid_until=$2, superseded_by=$3 WHERE id=$1",
                    other["id"], effective_at, new_row["id"],
                )
            resolution = "new_wins" if decision == "new" else "recency_tiebreak"
            cid = await _record_contradiction(
                conn, "graph_edge", old["id"], new_row["id"],
                MemorySource(old["source"]), source, resolution,
            )
            return row_to_edge(new_row), ContradictionOutcome("new", resolution, cid)

        # Old (stronger) source wins: keep it current, record the new claim as closed dissent.
        dissent = await _insert_edge(
            conn, src_id, dst_id, rel_type, props, source, confidence, memory_id,
            effective_at, valid_until=effective_at,
        )
        cid = await _record_contradiction(
            conn, "graph_edge", old["id"], dissent["id"],
            MemorySource(old["source"]), source, "old_wins",
        )
        return row_to_edge(old), ContradictionOutcome("old", "old_wins", cid)


async def _insert_edge(
    conn: Any,
    src_id: UUID,
    dst_id: UUID,
    rel_type: str,
    props: dict[str, Any] | None,
    source: MemorySource,
    confidence: float,
    memory_id: UUID | None,
    valid_from: datetime | None,
    valid_until: datetime | None = None,
) -> Any:
    row = await conn.fetchrow(
        "INSERT INTO graph_edge (src_id, dst_id, rel_type, props, source, confidence, "
        "  valid_from, valid_until, last_verified, memory_id) "
        "VALUES ($1,$2,$3,$4,$5::memory_source,$6, COALESCE($7::timestamptz, now()), $8, "
        "  COALESCE($7::timestamptz, now()), $9) RETURNING *",
        src_id, dst_id, rel_type, props or {}, source.value, confidence, valid_from,
        valid_until, memory_id,
    )
    await _note_evidence(conn, edge_id=row["id"], source=source, confidence=confidence, source_ref=memory_id)
    return row


async def _record_contradiction(
    conn: Any,
    subject_type: str,
    old_id: UUID,
    new_id: UUID,
    old_source: MemorySource,
    new_source: MemorySource,
    resolution: str,
) -> UUID:
    # A graph fact is VERIFIABLE — the twin re-observes the machine — so a priority-only resolution
    # of an observable slot stays 'open' until a live re-observation settles it (§20/§25).
    status, resolved_by = contradiction_lifecycle(old_source, new_source, verifiable=True)
    cid: UUID = await conn.fetchval(
        "INSERT INTO contradiction (subject_type, old_id, new_id, status, old_source, new_source, "
        "  old_priority, new_priority, resolution, resolved_by, resolved_at) "
        "VALUES ($1,$2,$3,$4::contradiction_status,$5::memory_source,$6::memory_source,$7,$8,$9,$10,"
        "  CASE WHEN $11 THEN now() ELSE NULL END) RETURNING id",
        subject_type, old_id, new_id, status, old_source.value, new_source.value,
        source_priority(old_source), source_priority(new_source), resolution, resolved_by,
        status != "open",
    )
    await conn.execute(
        "INSERT INTO event (event_type, subject_type, subject_id, payload) "
        "VALUES ('memory.contradiction', $1, $2, $3)",
        subject_type, old_id,
        {"resolution": resolution, "contradiction_id": str(cid), "status": status},
    )
    return cid


async def _verify_open_by_observation(
    conn: Any, src_id: UUID, rel_type: str, at: datetime
) -> None:
    """A live re-observation of this slot just happened — settle any OPEN contradiction about it by
    VERIFICATION (§20/§25 'then verify'). This is what closes the loop: a priority guess becomes a
    checked fact once the machine is actually re-inspected."""
    rows = await conn.fetch(
        "UPDATE contradiction SET status='resolved', resolved_by='verification', resolved_at=$3 "
        "WHERE status='open' AND subject_type='graph_edge' AND ("
        "  old_id IN (SELECT id FROM graph_edge WHERE src_id=$1 AND rel_type=$2) OR "
        "  new_id IN (SELECT id FROM graph_edge WHERE src_id=$1 AND rel_type=$2)) RETURNING id",
        src_id, rel_type, at,
    )
    for row in rows:
        await conn.execute(
            "INSERT INTO event (event_type, subject_type, subject_id, payload) "
            "VALUES ('memory.verified', 'graph_edge', $1, $2)",
            src_id, {"contradiction_id": str(row["id"]), "by": "observation"},
        )
