"""Deterministic external-tool discovery (spec §3/§28/§49).

Sali should know every tool installed on its machine. This finds them the way a shell does — by
walking PATH — with NO model involved (§28): "what binaries exist and what package owns them" is a
scan, not a question for Qwen. Each discovered binary becomes:
  * a row in the ``discovered_tool`` INVENTORY table (the authoritative catalog the runtime reads), and
  * an ``ext_tool`` node in the existing knowledge graph, hung off the machine by a ``has_tool`` edge
    (the reasoning surface later increments relate capabilities to).

Discovery reuses the twin's observe→diff→fold discipline: only genuinely new/removed tools are
itemized as events (a full first scan is summarized), so a periodic re-scan never floods the event
log (§57). Version/synopsis probing is deliberately deferred — running ``--version`` on ~2500
binaries every pass is exactly the expensive continuous work §28/§57 warn against; it belongs to a
later, lazy step.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sali.core.enums import MemorySource
from sali.core.toolvocab import NODE_EXT_TOOL, NODE_MACHINE, REL_HAS_TOOL, ext_tool_key
from sali.graph.writer import ensure_node, relate
from sali.obs.log import get_logger
from sali.twin.observers import _run, observe_machine

log = get_logger("sali.twin.tools")

_DPKG_BATCH = 400  # paths per `dpkg -S` call — one call resolves many, but keep argv bounded


@dataclass(slots=True)
class DiscoveryResult:
    machine_id: UUID
    total: int              # tools currently on the machine
    added: list[str]        # newly discovered since last scan
    removed: list[str]      # gone since last scan (marked unavailable, edge closed)


def observe_path_tools() -> dict[str, str]:
    """Every executable on PATH, name→absolute path, first-on-PATH wins (shell resolution order).
    Pure filesystem inspection; never raises."""
    found: dict[str, str] = {}
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory or not os.path.isdir(directory):
            continue
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            if entry.name in found:
                continue
            try:
                if entry.is_file(follow_symlinks=True) and os.access(entry.path, os.X_OK):
                    found[entry.name] = entry.path
            except OSError:
                continue
    return found


async def resolve_packages(paths: dict[str, str]) -> dict[str, str]:
    """Map tool name → owning OS package via a few bulk ``dpkg -S`` calls (one call resolves many).
    Locally-built binaries own no package and are simply omitted. Empty if dpkg is absent."""
    if not paths or shutil.which("dpkg") is None:
        return {}
    path_to_pkg: dict[str, str] = {}
    all_paths = list(paths.values())
    for i in range(0, len(all_paths), _DPKG_BATCH):
        out = await _run("dpkg", "-S", *all_paths[i : i + _DPKG_BATCH])
        for line in out.splitlines():
            pkg, sep, path = line.partition(": ")
            if not sep:
                continue
            path = path.strip()
            if path:  # "pkg1, pkg2: /path" on a diversion → take the first
                path_to_pkg[path] = pkg.split(",")[0].strip()
    return {name: path_to_pkg[p] for name, p in paths.items() if p in path_to_pkg}


def _tool_props(path: str, package: str | None) -> dict[str, Any]:
    props: dict[str, Any] = {"path": path}
    if package:
        props["package"] = package
    return props


async def _existing_tool_names(conn: Any, machine_id: UUID) -> set[str]:
    """Names of tools the graph currently records the machine as having (via has_tool edges)."""
    rows = await conn.fetch(
        "SELECT n.canonical_key FROM graph_edge e JOIN graph_node n ON n.id = e.dst_id "
        "WHERE e.src_id=$1 AND e.rel_type=$2 AND e.valid_until IS NULL AND e.superseded_by IS NULL "
        "  AND n.valid_until IS NULL",
        machine_id, REL_HAS_TOOL,
    )
    prefix = f"{NODE_EXT_TOOL}:"
    return {r["canonical_key"][len(prefix):] for r in rows if r["canonical_key"].startswith(prefix)}


async def sync_tools(
    conn: Any, found: dict[str, str], packages: dict[str, str],
    *, source: MemorySource = MemorySource.SYSTEM_OBSERVATION,
) -> DiscoveryResult:
    """Fold a discovered tool set into the inventory + graph and return what changed. Caller owns
    the transaction. Idempotent: re-running with the same set is a no-op diff."""
    key, mname, mprops = observe_machine()
    machine = await ensure_node(
        conn, node_type=NODE_MACHINE, name=mname, canonical_key=key, source=source, props=mprops)

    before = await _existing_tool_names(conn, machine.id)
    seen = set(found)
    added = sorted(seen - before)
    removed = sorted(before - seen)

    # Inventory upsert — the full current catalog, one stable row per tool name.
    if found:
        await conn.executemany(
            "INSERT INTO discovered_tool (name, path, package, source, available, last_seen) "
            "VALUES ($1,$2,$3,$4::memory_source, true, now()) "
            "ON CONFLICT (name) DO UPDATE SET path=EXCLUDED.path, package=EXCLUDED.package, "
            "  available=true, last_seen=now()",
            [(n, found[n], packages.get(n) or None, source.value) for n in found],
        )

    # Graph nodes + machine→has_tool edges only for NEW tools (existing ones already have them);
    # link the freshly-created node back to its inventory row.
    for name in added:
        node = await ensure_node(
            conn, node_type=NODE_EXT_TOOL, name=name, canonical_key=ext_tool_key(name),
            source=source, props=_tool_props(found[name], packages.get(name)))
        await relate(conn, src_id=machine.id, dst_id=node.id, rel_type=REL_HAS_TOOL, source=source)
        await conn.execute("UPDATE discovered_tool SET node_id=$1 WHERE name=$2", node.id, name)

    # A tool that's gone: mark it unavailable and end the machine's "has" edge (node stays as history).
    for name in removed:
        await _close_tool_edge(conn, machine.id, name)
        await conn.execute(
            "UPDATE discovered_tool SET available=false, last_seen=now() WHERE name=$1", name)

    await _emit_discovery(conn, machine.id, total=len(found), added=added, removed=removed,
                          first=not before)
    return DiscoveryResult(machine_id=machine.id, total=len(found), added=added, removed=removed)


async def discover_tools(
    conn: Any, *, source: MemorySource = MemorySource.SYSTEM_OBSERVATION
) -> DiscoveryResult:
    """Scan the live machine (PATH + dpkg) and fold the result. Caller owns the transaction."""
    found = observe_path_tools()
    packages = await resolve_packages(found)
    return await sync_tools(conn, found, packages, source=source)


async def _close_tool_edge(conn: Any, machine_id: UUID, name: str) -> None:
    await conn.execute(
        "UPDATE graph_edge SET valid_until = now() "
        "WHERE src_id=$1 AND rel_type=$2 AND valid_until IS NULL AND superseded_by IS NULL "
        "  AND dst_id = (SELECT id FROM graph_node WHERE canonical_key=$3 AND valid_until IS NULL LIMIT 1)",
        machine_id, REL_HAS_TOOL, ext_tool_key(name),
    )


async def _emit_discovery(
    conn: Any, machine_id: UUID, *, total: int, added: list[str], removed: list[str], first: bool
) -> None:
    await conn.execute(
        "INSERT INTO event (event_type, subject_type, subject_id, payload) "
        "VALUES ('tool.discovery_synced','machine',$1,$2)",
        machine_id, {"total": total, "added": len(added), "removed": len(removed)},
    )
    # First discovery is bulk — summarize, don't itemize (§57). Later, a genuinely new tool is worth
    # an event; a disappearance always is.
    if not first:
        for name in added:
            await conn.execute(
                "INSERT INTO event (event_type, subject_type, subject_id, payload) "
                "VALUES ('tool.discovered','machine',$1,$2)", machine_id, {"name": name})
    for name in removed:
        await conn.execute(
            "INSERT INTO event (event_type, subject_type, subject_id, payload) "
            "VALUES ('tool.removed','machine',$1,$2)", machine_id, {"name": name})
