"""Memory management: browsing, statistics, and the repairs a person performs on Sali's beliefs.

This is a projection and two repairs, NOT a second memory system. Everything that changes belief goes
through the one canonical writer: create/revise -> `writer.remember`, invalidate -> `writer.forget`,
verify -> `writer.reground`. What lives here is the read shape the app needs, plus the two operations the
writer has no home for because they are curation rather than cognition: restoring an invalidated memory,
and erasing one outright for a privacy request.

Three things measured on a scratch database seeded with 100,009 rows shaped this file:

1. A naive restore corrupts recall. `ux_memory_claim` is partial on `superseded_by IS NULL`, so clearing
   `valid_until` on a SUPERSEDED row does not trip the unique index — and both retrievers filter on
   `valid_until IS NULL` alone. The measured result was one claim returning two live values at once
   ("home city is Vienna" AND "home city is Sarajevo"). `restore` refuses that case.

2. A hard delete cannot simply DELETE. Three foreign keys are NO ACTION (`memory.superseded_by`,
   `graph_node.memory_id`, `graph_edge.memory_id`) and abort the statement outright; four more references
   carry no constraint at all and would dangle silently (`graph_evidence.source_ref`,
   `contradiction.old_id/new_id`, `capability.supported_by`, `stm_observation.source_ref`). Ghost-free
   means all seven, in one transaction.

3. An invalidated memory leaves the vector index for free: `ix_memory_embed_hnsw` is partial on
   `valid_until IS NULL AND embed_status = 'done'`. That is exactly why invalidate and delete are
   different verbs — and why delete has to remove the row itself.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sali.core.enums import MemoryLayer, MemorySource
from sali.memory import writer

# Every column the app renders, in one place, so list/detail/patch cannot drift apart.
_COLS = """
    m.id, m.layer::text AS layer, m.content, m.source::text AS source, m.scope,
    m.structured->>'kind' AS kind, m.structured->>'task_id' AS task_id,
    m.structured->>'forget_reason' AS forget_reason,
    m.confidence, m.importance, m.reliability, m.evidence_count, m.access_count,
    m.functional, m.claim_key, m.needs_grounding, m.freshness::text AS freshness, m.embed_status,
    (m.valid_until IS NULL) AS is_current, m.superseded_by, m.source_ref,
    m.valid_from, m.valid_until, m.first_seen, m.last_seen, m.last_verified, m.last_accessed,
    m.created_at, m.updated_at
"""


def _row(r: Any) -> dict[str, Any]:
    """One memory as the API shape. Ids become strings; no key is ever absent, only null."""
    out: dict[str, Any] = {}
    for k, v in dict(r).items():
        out[k] = str(v) if isinstance(v, UUID) else v
    for f in ("confidence", "importance", "reliability"):
        if out.get(f) is not None:
            out[f] = round(float(out[f]), 4)
    return out


async def list_memories(
    conn: Any, *, layer: str | None = None, source: str | None = None, kind: str | None = None,
    scope: str | None = None, q: str | None = None, state: str = "current",
    needs_grounding: bool | None = None, min_confidence: float | None = None,
    since: Any = None, until: Any = None, limit: int = 50,
    cursor_created_at: Any = None, cursor_id: UUID | None = None,
) -> dict[str, Any]:
    """Browse memory. KEYSET paginated, never OFFSET.

    Offset paging degrades linearly with depth and a total count is unbounded on a store meant to grow for
    years; a keyset seek is flat. `ix_memory_recent (created_at DESC, id DESC)` is what makes the ordering
    an index scan instead of a top-N heapsort — measured 16.1 ms -> 0.037 ms at 100k rows, and unchanged
    at page 900.
    """
    where: list[str] = []
    args: list[Any] = []

    def arg(v: Any) -> str:
        args.append(v)
        return f"${len(args)}"

    if state == "current":
        where.append("m.valid_until IS NULL")
    elif state == "invalid":
        where.append("m.valid_until IS NOT NULL")
    if layer:
        where.append(f"m.layer = {arg(layer)}::memory_layer")
    if source:
        where.append(f"m.source = {arg(source)}::memory_source")
    if kind:
        where.append(f"m.structured->>'kind' = {arg(kind)}")
    if scope:
        where.append(f"m.scope = {arg(scope)}")
    if q:
        where.append(f"m.content ILIKE {arg('%' + q + '%')}")
    if needs_grounding is not None:
        where.append(f"m.needs_grounding = {arg(needs_grounding)}")
    if min_confidence is not None:
        where.append(f"m.confidence >= {arg(float(min_confidence))}")
    if since is not None:
        where.append(f"m.created_at >= {arg(since)}")
    if until is not None:
        where.append(f"m.created_at <= {arg(until)}")
    if cursor_created_at is not None and cursor_id is not None:
        where.append(f"(m.created_at, m.id) < ({arg(cursor_created_at)}, {arg(cursor_id)})")

    clause = (" WHERE " + " AND ".join(where)) if where else ""
    take = max(1, min(int(limit), 200))
    rows = await conn.fetch(
        f"SELECT {_COLS} FROM memory m{clause} ORDER BY m.created_at DESC, m.id DESC LIMIT {arg(take + 1)}",
        *args,
    )
    has_more = len(rows) > take
    page = [_row(r) for r in rows[:take]]
    nxt = None
    if has_more and page:
        nxt = {"created_at": page[-1]["created_at"], "id": page[-1]["id"]}
    return {"memories": page, "has_more": has_more, "next_cursor": nxt}


async def stats(conn: Any) -> dict[str, Any]:
    """What the overview screen shows. Counts only — never content."""
    by_layer = await conn.fetch(
        "SELECT layer::text AS layer, count(*) AS n FROM memory "
        "WHERE valid_until IS NULL GROUP BY 1 ORDER BY n DESC")
    by_source = await conn.fetch(
        "SELECT source::text AS source, count(*) AS n FROM memory "
        "WHERE valid_until IS NULL GROUP BY 1 ORDER BY n DESC")
    totals = await conn.fetchrow(
        "SELECT count(*) FILTER (WHERE valid_until IS NULL) AS current, "
        "       count(*) FILTER (WHERE valid_until IS NOT NULL) AS invalidated, "
        "       count(*) FILTER (WHERE valid_until IS NULL AND needs_grounding) AS unverified, "
        "       count(*) FILTER (WHERE superseded_by IS NOT NULL) AS superseded, "
        "       count(*) FILTER (WHERE valid_until IS NULL AND embed_status <> 'done') AS unembedded, "
        "       count(*) AS total FROM memory")
    return {
        "totals": dict(totals),
        "by_layer": [dict(r) for r in by_layer],
        "by_source": [dict(r) for r in by_source],
    }


async def get_one(conn: Any, memory_id: UUID) -> dict[str, Any] | None:
    row = await conn.fetchrow(f"SELECT {_COLS} FROM memory m WHERE m.id = $1", memory_id)
    if row is None:
        return None
    out = _row(row)
    out["evidence"] = [
        dict(e) for e in await conn.fetch(
            "SELECT source::text AS source, confidence, note, created_at FROM memory_evidence "
            "WHERE memory_id = $1 ORDER BY created_at", memory_id)
    ]
    return out


async def restore(conn: Any, memory_id: UUID) -> dict[str, Any]:
    """Bring an invalidated memory back into current knowledge.

    Refuses a SUPERSEDED row. Restoring one does not trip `ux_memory_claim` (partial on
    `superseded_by IS NULL`) and the retrievers do not filter on it either, so the loser of a correction
    rejoins recall beside the winner and the same claim answers with two different values. Measured. The
    honest repair for that case is to re-state the belief, not to un-retire the contradicted one.
    """
    row = await conn.fetchrow(
        "SELECT id, valid_until, superseded_by FROM memory WHERE id = $1", memory_id)
    if row is None:
        return {"restored": False, "reason": "not_found"}
    if row["valid_until"] is None:
        return {"restored": False, "reason": "already_current"}
    if row["superseded_by"] is not None:
        return {"restored": False, "reason": "superseded"}
    await conn.execute(
        "UPDATE memory SET valid_until = NULL, updated_at = now(), "
        "  structured = structured - 'forget_reason' WHERE id = $1", memory_id)
    return {"restored": True}


async def revise(conn: Any, memory_id: UUID, *, content: str, reason: str) -> dict[str, Any]:
    """Replace what a memory says, as an authoritative correction from Almir.

    NOT `writer.remember` on its own. For a functional claim whose current row came from a
    `system_observation` (priority 100), `compare_sources` ranks that ABOVE `user_explicit` (80), so
    `_resolve_claim` takes the old-wins branch: the correction is filed as a closed dissent row and the
    original stays current — while the caller gets a 200 and the OLD text back. Measured: that is 8 of 22
    live memories today, and it is silent. A person correcting Sali is not evidence to be weighed against
    a filesystem scan; it is a decision. So the old row is retired FIRST, then the new one is written, and
    the supersession link is set explicitly.
    """
    old = await conn.fetchrow(
        "SELECT id, layer, content, functional, claim_key, scope, importance, valid_until "
        "FROM memory WHERE id = $1", memory_id)
    if old is None:
        return {"revised": False, "reason": "not_found"}
    if old["valid_until"] is not None:
        return {"revised": False, "reason": "not_current"}
    await writer.forget(conn, memory_id, reason=reason)
    fresh = await writer.remember(
        # asyncpg hands the enum back as a plain string; the writer wants the enum.
        conn, layer=MemoryLayer(old["layer"]), content=content, source=MemorySource.USER_EXPLICIT,
        obs_conf=0.95, importance=float(old["importance"]),
        functional=bool(old["functional"]), claim_key=old["claim_key"], scope=old["scope"],
        note=f"revised by Almir: {reason}"[:400],
    )
    await conn.execute("UPDATE memory SET superseded_by = $2 WHERE id = $1", memory_id, fresh.id)
    return {"revised": True, "old_id": str(memory_id), "new_id": str(fresh.id)}


async def hard_delete(conn: Any, memory_id: UUID, *, reason: str) -> dict[str, Any]:
    """Erase a memory outright. Irreversible; for a privacy request, not a correction.

    Seven references have to go with it or the delete either aborts or leaves a ghost — see the module
    docstring. The event log is append-only (an immutability trigger rejects UPDATE and DELETE on it), so
    history keeps the fact that a memory existed; it never keeps the text, and no `memory.*` payload
    carries content.
    """
    row = await conn.fetchrow(
        "SELECT id, layer::text AS layer, source::text AS source, superseded_by, "
        "       (valid_until IS NULL) AS was_current FROM memory WHERE id = $1", memory_id)
    if row is None:
        return {"deleted": False, "reason": "not_found"}

    removed: dict[str, int] = {}

    def n(result: Any) -> int:
        try:
            return int(str(result).split()[-1])
        except (ValueError, IndexError, AttributeError):
            return 0

    # Predecessors point at this row; re-point them at its own successor so the chain survives.
    removed["supersession_links_spliced"] = n(await conn.execute(
        "UPDATE memory SET superseded_by = $2 WHERE superseded_by = $1", memory_id, row["superseded_by"]))
    # NO ACTION FKs: unlink and close, rather than delete — other edges may still reference the node.
    removed["graph_nodes_unlinked"] = n(await conn.execute(
        "UPDATE graph_node SET memory_id = NULL, valid_until = coalesce(valid_until, now()) "
        "WHERE memory_id = $1", memory_id))
    removed["graph_edges_unlinked"] = n(await conn.execute(
        "UPDATE graph_edge SET memory_id = NULL, valid_until = coalesce(valid_until, now()) "
        "WHERE memory_id = $1", memory_id))
    # Unconstrained references that would dangle silently.
    removed["graph_evidence_unlinked"] = n(await conn.execute(
        "UPDATE graph_evidence SET source_ref = NULL WHERE source_ref = $1", memory_id))
    removed["contradiction_records"] = n(await conn.execute(
        "DELETE FROM contradiction WHERE old_id = $1 OR new_id = $1", memory_id))
    removed["working_observations"] = n(await conn.execute(
        "DELETE FROM stm_observation WHERE source_ref = $1", memory_id))
    # memory_evidence is ON DELETE CASCADE; the embedding leaves the partial HNSW index with the row.
    removed["memory_rows"] = n(await conn.execute("DELETE FROM memory WHERE id = $1", memory_id))
    await conn.execute(
        "INSERT INTO event (event_type, subject_type, subject_id, payload) "
        "VALUES ('memory.deleted','memory',$1,$2)",
        memory_id,
        {"layer": row["layer"], "source": row["source"], "was_current": row["was_current"],
         "reason": (reason or "")[:200]},
    )
    return {"deleted": removed["memory_rows"] == 1, "was_current": row["was_current"],
            "layer": row["layer"], "source": row["source"], "removed": removed}
