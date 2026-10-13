"""TwinService — build/refresh the desktop twin and render it structurally.

``refresh`` runs the deterministic observers and folds the snapshot into the graph in one
transaction. ``tree`` reads the twin back *from the graph* (so it reflects persisted, temporal
state) and renders the structural picture the spec asks for (§14) — Hardware / Software /
Models / Projects under the machine.

Snapshot dedup (2026-09-13): unchanged snapshots skip memory writes entirely, so system_env
memories are only corroborated when the machine state actually changes. This eliminates the
5-minute write churn that kept system_env freshness at 1.0 always, making them outrank
episodic/semantic memories in retrieval.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
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
    def __init__(self, pool: Any, memory: MemoryService | None = None, settings: Any = None) -> None:
        self.pool = pool
        self.memory = memory  # when given, refresh also writes+embeds system-env memories
        self.settings = settings  # when given, refresh also links agent:sali into its world (§1)
        self._last_snapshot_hash: str = ""

    async def refresh(self, *, exclude_projects: tuple[str, ...] = ()) -> SyncResult:
        """Observe the machine, reconcile the snapshot into the graph, and (if a memory service
        is wired) record the same facts as retrievable system-env memories — one transaction for
        the writes, then embedding so the agent can ground machine questions from the twin."""
        snapshot = await build_snapshot(exclude_projects=exclude_projects)
        # Hash the snapshot to detect no-change cycles. Unchanged snapshots skip memory writes
        # so system_env memories are only corroborated when the machine state actually changes.
        snap_hash = hashlib.sha256(
            json.dumps(dataclasses.asdict(snapshot), sort_keys=True, default=str).encode()
        ).hexdigest()
        unchanged = snap_hash == self._last_snapshot_hash
        self._last_snapshot_hash = snap_hash

        async with self.pool.acquire() as conn, conn.transaction():
            result = await sync_snapshot(conn, snapshot)
            if self.memory is not None and not unchanged:
                await write_twin_memories(conn, snapshot)
            if self.settings is not None:  # §1: stitch the agent to machine/model/user/workspace/source
                from sali.twin.self_link import link_self

                perms = self.settings.permissions
                # Use the LIVE active model (a swap updates sali_state), falling back to config, so a
                # re-sync affirms the model Sali runs NOW and never clobbers a swap with the config value.
                active_model = await conn.fetchval(
                    "SELECT active_chat_model FROM sali_state WHERE id = true")
                # The configured name is backend-specific: the llama.cpp server serves the model
                # under llamacpp_model_alias; chat_model is the Ollama-era tag. Using the wrong one
                # would mint a model node that exists nowhere (drift against the observed artifact).
                configured = (self.settings.model.llamacpp_model_alias
                              if self.settings.model.provider == "llamacpp"
                              else self.settings.model.chat_model)
                await link_self(
                    conn, machine_id=result.machine_id,
                    model_name=(active_model or configured),
                    workspace=perms.workspace,
                    source_dir=(perms.fs_readonly[0] if perms.fs_readonly else ""))
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
