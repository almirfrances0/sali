"""Reconcile a twin snapshot into the temporal knowledge graph (spec §14 + §16 seed).

The machine and everything on it become graph nodes (``ensure_node`` — stable identity) and
relationships (``relate`` — machine *has*/*runs*/*hosts* X); changing attributes (a version
bump) are refreshed new-wins. Because the graph is temporal, structure is remembered over time.

This is also the cheap first layer of event-driven observation (§16): each sync diffs against
what the graph already knew and emits ``twin.entity_added`` / ``twin.entity_removed`` for
*meaningful* structural changes — not a flood of raw events. Bulk first-discovery is summarized,
not itemized, so only genuine later changes cost attention.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sali.core.enums import MemorySource
from sali.graph.writer import ensure_node, refresh_props, relate
from sali.obs.log import get_logger
from sali.twin.model import TwinSnapshot

log = get_logger("sali.twin.sync")


@dataclass(slots=True)
class SyncResult:
    machine_id: UUID
    entities: int
    added: list[str]
    removed: list[str]


async def _existing_entity_keys(conn: Any, machine_id: UUID) -> set[str]:
    rows = await conn.fetch(
        "SELECT n.canonical_key FROM graph_edge e JOIN graph_node n ON n.id = e.dst_id "
        "WHERE e.src_id=$1 AND e.valid_until IS NULL AND e.superseded_by IS NULL "
        "  AND n.valid_until IS NULL",
        machine_id,
    )
    return {r["canonical_key"] for r in rows}


async def sync_snapshot(
    conn: Any, snapshot: TwinSnapshot, *, source: MemorySource = MemorySource.SYSTEM_OBSERVATION
) -> SyncResult:
    """Fold ``snapshot`` into the graph and return what changed. Caller owns the transaction."""
    machine = await ensure_node(
        conn, node_type="machine", name=snapshot.machine_name,
        canonical_key=snapshot.machine_key, source=source, props=snapshot.machine_props,
    )
    await refresh_props(conn, machine.id, snapshot.machine_props)

    before = await _existing_entity_keys(conn, machine.id)
    seen: set[str] = set()
    for ent in snapshot.entities:
        node = await ensure_node(
            conn, node_type=ent.kind, name=ent.name, canonical_key=ent.key,
            source=source, props=ent.props,
        )
        await refresh_props(conn, node.id, ent.props)
        await relate(conn, src_id=machine.id, dst_id=node.id, rel_type=ent.relation, source=source)
        seen.add(ent.key)

    added = sorted(seen - before)
    removed = sorted(before - seen)
    await _emit(conn, "twin.synced", machine.id,
                {"entities": len(snapshot.entities), "added": len(added), "removed": len(removed)})
    # First discovery is bulk — don't itemize it as events; only surface genuine later changes.
    if before:
        for key in added:
            await _emit(conn, "twin.entity_added", machine.id, {"key": key})
    for key in removed:  # a disappearance is always worth an event
        await _emit(conn, "twin.entity_removed", machine.id, {"key": key})
    return SyncResult(machine_id=machine.id, entities=len(snapshot.entities),
                      added=added, removed=removed)


async def _emit(conn: Any, event_type: str, subject_id: UUID, payload: dict[str, Any]) -> None:
    await conn.execute(
        "INSERT INTO event (event_type, subject_type, subject_id, payload) "
        "VALUES ($1,'machine',$2,$3)",
        event_type, subject_id, payload,
    )
