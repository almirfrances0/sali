"""The Desktop Digital Twin (§14–16): deterministic observation, graph reconciliation, diff."""

from __future__ import annotations

from typing import Any

import pytest

from sali.twin.memories import build_twin_memories, write_twin_memories
from sali.twin.model import TwinEntity, TwinSnapshot
from sali.twin.observers import (
    observe_cpu,
    observe_machine,
    observe_memory,
    observe_network,
    observe_python_envs,
    observe_services,
)
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


async def test_richer_observers_are_well_typed_and_defensive() -> None:
    # Env-dependent (systemctl/network/venvs may or may not be present) — assert only shape.
    for ents, kind in (
        (await observe_services(), "service"),
        (await observe_network(), "network"),
        (observe_python_envs(), "environment"),
    ):
        assert isinstance(ents, list)
        assert all(e.kind == kind and e.key and e.name for e in ents)


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

    # A removed entity's edge is closed, so it no longer shows as current (node stays as history).
    current_keys = await db_conn.fetch(
        "SELECT n.canonical_key FROM graph_edge e JOIN graph_node n ON n.id=e.dst_id "
        "WHERE e.src_id=$1 AND e.valid_until IS NULL", machine["id"]
    )
    keys = {r["canonical_key"] for r in current_keys}
    assert "project:sali" not in keys and "model:sali:latest" in keys

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
        TwinEntity("service", "service:nginx", "nginx (running)", {"state": "active"}, "runs"),
        TwinEntity("container", "container:db", "db (postgres:18)", {"image": "postgres:18"}, "runs"),
        TwinEntity("network", "net:eth0", "eth0 (up)", {"ip": "192.168.1.5/24"}, "has"),
    ])
    mems = dict(build_twin_memories(snap))
    assert "RTX 4070" in mems["twin:machine"] and "i9-11900K" in mems["twin:machine"]
    assert "GPU:" in mems["twin:machine"] and "CPU:" in mems["twin:machine"]  # keyword anchors
    assert "Ollama 0.32" in mems["twin:software"]
    assert "sali:latest" in mems["twin:models"]
    assert "/home/almir/Desktop/sali" in mems["twin:projects"]
    # the previously-missing volatile facets now become retrievable memories
    assert "nginx" in mems["twin:services"]
    assert "db" in mems["twin:containers"]
    assert "192.168.1.5/24" in mems["twin:network"] and "IP" in mems["twin:network"]


def test_build_twin_memories_emits_empty_membership_facets_for_retraction() -> None:
    # No services/containers/network observed → still emit an explicit "none" so a functional
    # re-observation retracts a previously-true value instead of leaving it current forever.
    mems = dict(build_twin_memories(_snap([TwinEntity("hardware", "hw:gpu", "RTX 4070", {}, "has")])))
    assert "No Docker containers" in mems["twin:containers"]
    assert "twin:services" in mems and "twin:network" in mems


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


async def test_daemon_on_tick_fires_every_cycle() -> None:
    from uuid import uuid4

    from sali.twin.daemon import TwinDaemon
    from sali.twin.sync import SyncResult

    stub = _StubService([SyncResult(uuid4(), 1, [], []), SyncResult(uuid4(), 1, [], [])])
    ticks: list[int] = []

    async def on_tick(cycle: int) -> None:
        ticks.append(cycle)

    await TwinDaemon(stub, interval=0.01).run(max_cycles=2, on_tick=on_tick)
    assert ticks == [1, 2]  # learning piggybacks on this — one call per observe cycle


async def test_daemon_survives_a_failing_cycle() -> None:
    from uuid import uuid4

    from sali.twin.daemon import TwinDaemon
    from sali.twin.sync import SyncResult

    stub = _StubService([RuntimeError("observe blew up"), SyncResult(uuid4(), 1, [], [])])
    daemon = TwinDaemon(stub, interval=0.01)
    ran = await daemon.run(max_cycles=1)
    assert ran == 1 and stub.calls == 2  # it retried after the failure instead of dying


# ---- the 'interpret' branch: Sali notices changes on its next turn (§16) -----------------------
async def test_awareness_surfaces_changes_then_acknowledges(db_conn: Any) -> None:
    from sali.twin.awareness import acknowledge, unacknowledged_changes

    await sync_snapshot(db_conn, _snap([TwinEntity("software", "software:git", "Git 2.53", {}, "runs")]))
    # A later observation: git's gone, docker appeared → both become change events.
    await sync_snapshot(db_conn, _snap([TwinEntity("software", "software:docker", "Docker 27", {}, "runs")]))

    phrases, through = await unacknowledged_changes(db_conn)
    assert any("Docker 27" in p and "installed" in p for p in phrases)
    assert any("Git 2.53" in p and "gone" in p for p in phrases)

    await acknowledge(db_conn, through)
    again, _ = await unacknowledged_changes(db_conn)
    assert again == []  # surfaced once, then quiet


async def test_desktop_observations_persist_and_surface_then_acknowledge(db_conn: Any) -> None:
    # sali3 Phase 5 fusion: the continuous engine's observations reach the loop's awareness.
    from datetime import UTC, datetime

    from sali.events.base import EventKind, Observation
    from sali.events.sink import DbObservationSink
    from sali.twin.awareness import acknowledge_observations, unacknowledged_observations

    class _Acq:
        def __init__(self, c: Any) -> None:
            self.c = c

        async def __aenter__(self) -> Any:
            return self.c

        async def __aexit__(self, *a: Any) -> bool:
            return False

    class _Pool:
        def __init__(self, c: Any) -> None:
            self.c = c

        def acquire(self) -> Any:
            return _Acq(self.c)

    t = datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC)
    sink = DbObservationSink(_Pool(db_conn), min_importance=0.6)
    await sink.observe(Observation(EventKind.FILE_MODIFIED, "modified main.py", 0.8, 1, t, t))
    await sink.observe(Observation(EventKind.FILE_MODIFIED, "modified junk", 0.3, 1, t, t))  # too low

    phrases, through = await unacknowledged_observations(db_conn)
    assert phrases == ["modified main.py"]  # only the meaningful one was persisted + surfaced

    await acknowledge_observations(db_conn, through)
    again, _ = await unacknowledged_observations(db_conn)
    assert again == []  # surfaced once, then quiet


async def test_awareness_dedups_add_then_remove(db_conn: Any) -> None:
    from sali.twin.awareness import unacknowledged_changes

    base = TwinEntity("software", "software:git", "Git", {}, "runs")
    tmp = TwinEntity("project", "project:tmp", "tmp", {}, "hosts")
    await sync_snapshot(db_conn, _snap([base]))
    await sync_snapshot(db_conn, _snap([base, tmp]))  # tmp appears
    await sync_snapshot(db_conn, _snap([base]))  # tmp gone again
    phrases, _ = await unacknowledged_changes(db_conn)
    # The add and the remove of project:tmp collapse to its final state (gone), not two mentions.
    tmp_mentions = [p for p in phrases if "tmp" in p]
    assert len(tmp_mentions) == 1 and "gone" in tmp_mentions[0]


async def test_refresh_updates_node_name_on_version_bump(db_conn: Any) -> None:
    await sync_snapshot(db_conn, _snap([
        TwinEntity("software", "software:ollama", "Ollama 0.32", {"version": "0.32"}, "runs")]))
    await sync_snapshot(db_conn, _snap([
        TwinEntity("software", "software:ollama", "Ollama 0.33", {"version": "0.33"}, "runs")]))
    name = await db_conn.fetchval(
        "SELECT name FROM graph_node WHERE canonical_key='software:ollama' AND valid_until IS NULL"
    )
    assert name == "Ollama 0.33"  # the tree won't render a stale version forever


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
