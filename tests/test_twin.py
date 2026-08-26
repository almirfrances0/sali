"""The Desktop Digital Twin (§14–16): deterministic observation, graph reconciliation, diff."""

from __future__ import annotations

from typing import Any

import pytest

from sali.twin.memories import build_twin_memories, write_twin_memories
from sali.twin.model import TwinEntity, TwinSnapshot
from sali.twin.observers import observe_cpu, observe_machine, observe_memory
from sali.twin.service import TwinService
from sali.twin.sync import sync_snapshot


# ---- deterministic observers (no model, no DB) ------------------------------------------------
def test_observe_machine_identity() -> None:
    key, name, props = observe_machine()
    assert key.startswith("machine:") and name
    assert "hostname" in props and "arch" in props


def test_observe_memory_and_cpu_from_proc() -> None:
    mem = observe_memory()  # /proc/meminfo exists on every Linux box
    assert mem is not None and mem.key == "hw:memory" and mem.props["total_gib"] > 0
    cpu = observe_cpu()
    if cpu is not None:  # present on real hardware; tolerate odd/container CPUs
        assert cpu.key == "hw:cpu" and cpu.kind == "hardware"


# ---- graph reconciliation + event-driven diff -------------------------------------------------
def _snap(entities: list[TwinEntity]) -> TwinSnapshot:
    return TwinSnapshot(machine_key="machine:test", machine_name="Test Box",
                        machine_props={"arch": "x86_64", "kernel": "7.0.0"}, entities=entities)


pytestmark = pytest.mark.db


async def test_sync_populates_graph_and_diffs_changes(db_conn: Any) -> None:
    first = _snap([
        TwinEntity("hardware", "hw:gpu", "RTX 4070", {"vram": "12 GiB"}, "has"),
        TwinEntity("software", "software:ollama", "Ollama 0.32", {"version": "0.32"}, "runs"),
        TwinEntity("project", "project:sali", "sali", {"branch": "main"}, "hosts"),
    ])
    r1 = await sync_snapshot(db_conn, first)
    assert r1.entities == 3 and not r1.removed  # first sync: bulk discovery, nothing "removed"

    # The machine node and its relationships now exist in the graph.
    machine = await db_conn.fetchrow(
        "SELECT id, props FROM graph_node WHERE canonical_key='machine:test' AND valid_until IS NULL"
    )
    assert machine is not None and machine["props"]["kernel"] == "7.0.0"
    edges = await db_conn.fetchval(
        "SELECT count(*) FROM graph_edge e JOIN graph_node n ON n.id=e.dst_id "
        "WHERE e.src_id=$1 AND e.valid_until IS NULL", machine["id"]
    )
    assert edges == 3

    # A second observation: ollama upgraded (prop refresh), a model appeared, the project is gone.
    second = _snap([
        TwinEntity("hardware", "hw:gpu", "RTX 4070", {"vram": "12 GiB"}, "has"),
        TwinEntity("software", "software:ollama", "Ollama 0.33", {"version": "0.33"}, "runs"),
        TwinEntity("model", "model:sali:latest", "sali:latest", {"size": "20 GB"}, "has_model"),
    ])
    r2 = await sync_snapshot(db_conn, second)
    assert "model:sali:latest" in r2.added  # genuinely new → surfaced
    assert "project:sali" in r2.removed  # disappeared → surfaced

    # The refreshed version won new-wins.
    ver = await db_conn.fetchval(
        "SELECT props->>'version' FROM graph_node "
        "WHERE canonical_key='software:ollama' AND valid_until IS NULL"
    )
    assert ver == "0.33"

    # Change events were journaled (not itemized on first sync; itemized on the second).
    added_events = await db_conn.fetchval(
        "SELECT count(*) FROM event WHERE event_type='twin.entity_added'"
    )
    removed_events = await db_conn.fetchval(
        "SELECT count(*) FROM event WHERE event_type='twin.entity_removed'"
    )
    assert added_events == 1 and removed_events == 1


def test_build_twin_memories_covers_facets() -> None:
    snap = _snap([
        TwinEntity("hardware", "hw:cpu", "i9-11900K", {}, "has"),
        TwinEntity("hardware", "hw:gpu", "RTX 4070", {}, "has"),
        TwinEntity("software", "software:ollama", "Ollama 0.32", {}, "runs"),
        TwinEntity("model", "model:sali:latest", "sali:latest", {}, "has_model"),
        TwinEntity("project", "project:sali", "sali", {"path": "/home/almir/Desktop/sali"}, "hosts"),
    ])
    mems = dict(build_twin_memories(snap))
    assert "RTX 4070" in mems["twin:machine"] and "i9-11900K" in mems["twin:machine"]
    assert "Ollama 0.32" in mems["twin:software"]
    assert "sali:latest" in mems["twin:models"]
    assert "/home/almir/Desktop/sali" in mems["twin:projects"]


async def test_twin_memories_are_functional_and_stay_current(db_conn: Any) -> None:
    # Re-observation supersedes the prior value (functional claim) — no duplicate pile-up.
    await write_twin_memories(db_conn, _snap([TwinEntity("hardware", "hw:gpu", "RTX 4070", {}, "has")]))
    await write_twin_memories(db_conn, _snap([TwinEntity("hardware", "hw:gpu", "RTX 5090", {}, "has")]))
    rows = await db_conn.fetch(
        "SELECT content FROM memory WHERE claim_key='twin:machine' AND valid_until IS NULL"
    )
    assert len(rows) == 1  # exactly one current machine fact
    assert "RTX 5090" in rows[0]["content"] and "RTX 4070" not in rows[0]["content"]


# ---- event-driven observation daemon (§16) ----------------------------------------------------
class _StubService:
    def __init__(self, results: list[Any]) -> None:
        self.results = results
        self.calls = 0

    async def refresh(self, *, exclude_projects: tuple[str, ...] = ()) -> Any:
        r = self.results[self.calls]
        self.calls += 1
        if isinstance(r, Exception):
            raise r
        return r


async def test_daemon_surfaces_only_meaningful_changes() -> None:
    from uuid import uuid4

    from sali.twin.daemon import TwinDaemon
    from sali.twin.sync import SyncResult

    stub = _StubService([
        SyncResult(uuid4(), 3, [], []),                 # cycle 1: nothing changed → quiet
        SyncResult(uuid4(), 4, ["software:htop"], []),  # cycle 2: a package appeared → surfaced
    ])
    changes: list[list[str]] = []

    async def on_change(r: Any) -> None:
        changes.append(r.added)

    daemon = TwinDaemon(stub, interval=0.01)
    ran = await daemon.run(max_cycles=2, on_change=on_change)
    assert ran == 2
    assert changes == [["software:htop"]]  # the no-change cycle did NOT fire on_change


async def test_daemon_survives_a_failing_cycle() -> None:
    from uuid import uuid4

    from sali.twin.daemon import TwinDaemon
    from sali.twin.sync import SyncResult

    stub = _StubService([RuntimeError("observe blew up"), SyncResult(uuid4(), 1, [], [])])
    daemon = TwinDaemon(stub, interval=0.01)
    ran = await daemon.run(max_cycles=1)
    assert ran == 1 and stub.calls == 2  # it retried after the failure instead of dying


async def test_tree_renders_structure(live_pool: Any) -> None:
    snap = _snap([
        TwinEntity("hardware", "hw:gpu", "RTX 4070", {}, "has"),
        TwinEntity("software", "software:ollama", "Ollama 0.32", {}, "runs"),
        TwinEntity("project", "project:sali", "sali", {}, "hosts"),
    ])
    async with live_pool.acquire() as c, c.transaction():
        await sync_snapshot(c, snap)
    tree = await TwinService(live_pool).tree()
    assert "Test Box" in tree
    assert "Hardware" in tree and "RTX 4070" in tree
    assert "Software" in tree and "Projects" in tree
