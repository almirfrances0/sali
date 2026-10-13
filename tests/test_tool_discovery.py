"""Tool Intelligence — Increment 2: deterministic tool discovery → graph + inventory (§3/§28/§49).

Verifies the observe→diff→fold discipline: PATH tools become discovered_tool rows + ext_tool graph
nodes hung off the machine, re-scans diff cleanly, a vanished tool is retired (not deleted), and —
critically — a normal twin sync does NOT clobber the has_tool edges (the fragmentation guard).
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.core.toolvocab import REL_HAS_TOOL, ext_tool_key
from sali.twin import tools
from sali.twin.model import TwinEntity, TwinSnapshot
from sali.twin.observers import observe_machine
from sali.twin.sync import sync_snapshot

pytestmark = pytest.mark.db


async def _tool_edge_open(conn: Any, machine_id: Any, name: str) -> bool:
    return bool(await conn.fetchval(
        "SELECT 1 FROM graph_edge e JOIN graph_node n ON n.id=e.dst_id "
        "WHERE e.src_id=$1 AND e.rel_type=$2 AND e.valid_until IS NULL AND n.canonical_key=$3",
        machine_id, REL_HAS_TOOL, ext_tool_key(name)))


async def test_sync_tools_populates_inventory_and_graph(db_conn: Any) -> None:
    found = {"nmap": "/usr/bin/nmap", "rg": "/usr/bin/rg"}
    packages = {"nmap": "nmap", "rg": "ripgrep"}

    result = await tools.sync_tools(db_conn, found, packages)

    assert result.total == 2 and set(result.added) == {"nmap", "rg"} and result.removed == []
    # inventory rows — available, path/package, and linked to their graph node
    rows = {r["name"]: r for r in await db_conn.fetch(
        "SELECT name, path, package, available, node_id FROM discovered_tool ORDER BY name")}
    assert set(rows) == {"nmap", "rg"}
    assert rows["nmap"]["package"] == "nmap" and rows["nmap"]["available"] is True
    assert rows["nmap"]["node_id"] is not None
    # graph: machine has_tool ext_tool:nmap
    assert await _tool_edge_open(db_conn, result.machine_id, "nmap")
    node = await db_conn.fetchrow(
        "SELECT node_type, props FROM graph_node WHERE canonical_key=$1 AND valid_until IS NULL",
        ext_tool_key("nmap"))
    assert node["node_type"] == "ext_tool" and node["props"]["package"] == "nmap"


async def test_rediscovery_is_idempotent(db_conn: Any) -> None:
    found = {"jq": "/usr/bin/jq"}
    first = await tools.sync_tools(db_conn, found, {})
    second = await tools.sync_tools(db_conn, found, {})

    assert first.added == ["jq"] and second.added == [] and second.removed == []
    assert await db_conn.fetchval("SELECT count(*) FROM discovered_tool WHERE name='jq'") == 1
    assert await db_conn.fetchval(
        "SELECT count(*) FROM graph_node WHERE canonical_key=$1 AND valid_until IS NULL",
        ext_tool_key("jq")) == 1  # no duplicate node


async def test_removed_tool_is_retired_then_can_return(db_conn: Any) -> None:
    m = (await tools.sync_tools(db_conn, {"a": "/usr/bin/a", "b": "/usr/bin/b"}, {})).machine_id

    gone = await tools.sync_tools(db_conn, {"a": "/usr/bin/a"}, {})
    assert gone.removed == ["b"]
    assert await db_conn.fetchval("SELECT available FROM discovered_tool WHERE name='b'") is False
    assert not await _tool_edge_open(db_conn, m, "b")   # edge closed
    assert await _tool_edge_open(db_conn, m, "a")       # survivor intact

    back = await tools.sync_tools(db_conn, {"a": "/usr/bin/a", "b": "/usr/bin/b"}, {})
    assert back.added == ["b"]
    assert await db_conn.fetchval("SELECT available FROM discovered_tool WHERE name='b'") is True
    assert await _tool_edge_open(db_conn, m, "b")       # edge re-opened


async def test_twin_sync_does_not_close_tool_edges(db_conn: Any) -> None:
    # THE fragmentation guard: tool discovery and the twin both hang children off the same machine
    # node. A twin sync must reconcile only ITS relations and leave has_tool edges alone.
    m = (await tools.sync_tools(db_conn, {"nmap": "/usr/bin/nmap"}, {})).machine_id

    key, name, mprops = observe_machine()
    snap = TwinSnapshot(machine_key=key, machine_name=name, machine_props=mprops,
                        entities=[TwinEntity(kind="software", key="software:git", name="Git 2.4",
                                             props={}, relation="runs")])
    await sync_snapshot(db_conn, snap)

    assert await _tool_edge_open(db_conn, m, "nmap")  # tool edge untouched by the twin
    # and the twin still manages its own child
    assert await db_conn.fetchval(
        "SELECT 1 FROM graph_node WHERE canonical_key='software:git' AND valid_until IS NULL")


def test_observe_path_tools_finds_real_binaries() -> None:
    found = tools.observe_path_tools()
    assert "sh" in found and found["sh"]  # a POSIX shell is always on PATH
    assert all(v.startswith("/") for v in found.values())  # absolute paths
