"""TwinService — build/refresh the desktop twin and render it structurally.

``refresh`` runs the deterministic observers and folds the snapshot into the graph in one
transaction. ``tree`` reads the twin back *from the graph* (so it reflects persisted, temporal
state) and renders the structural picture the spec asks for (§14) — Hardware / Software /
Models / Projects under the machine.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sali.obs.log import get_logger
from sali.twin.memories import write_twin_memories
from sali.twin.observers import build_snapshot
from sali.twin.sync import SyncResult, sync_snapshot

if TYPE_CHECKING:
    from sali.memory.service import MemoryService

log = get_logger("sali.twin.service")

# How each node_type is grouped and labelled when rendering the tree.
_GROUPS: tuple[tuple[str, str], ...] = (
    ("hardware", "Hardware"),
    ("software", "Software"),
    ("service", "Services"),
    ("container", "Containers"),
    ("model", "Models"),
    ("environment", "Environments"),
    ("network", "Network"),
    ("project", "Projects"),
)


class TwinService:
    def __init__(self, pool: Any, memory: MemoryService | None = None) -> None:
        self.pool = pool
        self.memory = memory  # when given, refresh also writes+embeds system-env memories

    async def refresh(self, *, exclude_projects: tuple[str, ...] = ()) -> SyncResult:
        """Observe the machine, reconcile the snapshot into the graph, and (if a memory service
        is wired) record the same facts as retrievable system-env memories — one transaction for
        the writes, then embedding so the agent can ground machine questions from the twin."""
        snapshot = await build_snapshot(exclude_projects=exclude_projects)
        async with self.pool.acquire() as conn, conn.transaction():
            result = await sync_snapshot(conn, snapshot)
            if self.memory is not None:
                await write_twin_memories(conn, snapshot)
        if self.memory is not None:
            await self.memory.embed_pending()  # make the new/updated facts searchable
        log.info("twin_refreshed", entities=result.entities,
                 added=len(result.added), removed=len(result.removed))
        return result

    async def tree(self) -> str:
        """Render the current twin from the graph as a structural tree, or a hint if empty."""
        async with self.pool.acquire() as conn:
            machine = await conn.fetchrow(
                "SELECT id, name, props FROM graph_node "
                "WHERE node_type='machine' AND valid_until IS NULL "
                "ORDER BY last_seen DESC LIMIT 1"
            )
            if machine is None:
                return "No twin yet — run `sali twin --refresh` to build it."
            rows = await conn.fetch(
                "SELECT n.node_type, n.name, n.props FROM graph_edge e "
                "JOIN graph_node n ON n.id = e.dst_id "
                "WHERE e.src_id=$1 AND e.valid_until IS NULL AND e.superseded_by IS NULL "
                "  AND n.valid_until IS NULL ORDER BY n.node_type, n.name",
                machine["id"],
            )
        props = machine["props"] or {}
        lines = [str(machine["name"])]
        detail = " · ".join(
            f"{k}: {props[k]}" for k in ("kernel", "arch") if props.get(k)
        )
        if detail:
            lines.append(f"  ({detail})")
        by_kind: dict[str, list[Any]] = {}
        for row in rows:
            by_kind.setdefault(row["node_type"], []).append(row)
        for kind, label in _GROUPS:
            items = by_kind.get(kind, [])
            if not items:
                continue
            lines.append(f"├── {label}")
            for row in items:
                lines.append(f"│   ├── {row['name']}")
        return "\n".join(lines)
