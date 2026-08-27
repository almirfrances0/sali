"""Temporal knowledge graph: traversal (test 2), as-of (test 3), contradictions (test 4)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from sali.core.enums import MemorySource
from sali.graph import traverse, writer

pytestmark = pytest.mark.db


async def _node(conn: Any, key: str, name: str, node_type: str = "entity") -> Any:
    return await writer.ensure_node(
        conn, node_type=node_type, name=name, canonical_key=key, source=MemorySource.USER_EXPLICIT
    )


async def test_multi_hop_traversal(db_conn: Any) -> None:
    almir = await _node(db_conn, "person:almir", "Almir", "person")
    proj = await _node(db_conn, "project:x", "Project X", "project")
    vps = await _node(db_conn, "host:vps01", "VPS-01", "host")
    docker = await _node(db_conn, "svc:docker", "Docker", "service")
    await writer.relate(db_conn, src_id=almir.id, dst_id=proj.id, rel_type="develops",
                        source=MemorySource.USER_EXPLICIT)
    await writer.relate(db_conn, src_id=proj.id, dst_id=vps.id, rel_type="deployed_on",
                        source=MemorySource.USER_EXPLICIT)
    await writer.relate(db_conn, src_id=vps.id, dst_id=docker.id, rel_type="runs",
                        source=MemorySource.USER_EXPLICIT)

    reached = await traverse.traverse(db_conn, almir.id, max_depth=4)
    names = {r["name"] for r in reached}
    assert {"Project X", "VPS-01", "Docker"} <= names
    assert next(r for r in reached if r["name"] == "Docker")["depth"] == 3


async def test_cycle_guard(db_conn: Any) -> None:
    a = await _node(db_conn, "c:a", "A")
    b = await _node(db_conn, "c:b", "B")
    await writer.relate(db_conn, src_id=a.id, dst_id=b.id, rel_type="k", source=MemorySource.USER_EXPLICIT)
    await writer.relate(db_conn, src_id=b.id, dst_id=a.id, rel_type="k", source=MemorySource.USER_EXPLICIT)
    reached = await traverse.traverse(db_conn, a.id, max_depth=4)  # must terminate, not loop
    # B is reachable from A; the cycle guard stops A from being re-reached through B→A.
    assert {r["name"] for r in reached} == {"B"}


async def test_as_of_temporal(db_conn: Any) -> None:
    t1 = datetime(2026, 1, 1, tzinfo=UTC)
    t2 = datetime(2026, 6, 1, tzinfo=UTC)
    desktop = await _node(db_conn, "host:kali", "Kali Desktop", "host")
    qwen = await _node(db_conn, "model:qwen", "Qwen 35B", "model")
    llama = await _node(db_conn, "model:llama", "Llama 8B", "model")
    await writer.set_fact(db_conn, src_id=desktop.id, rel_type="uses_model", dst_id=qwen.id,
                          source=MemorySource.USER_EXPLICIT, at=t1)
    await writer.set_fact(db_conn, src_id=desktop.id, rel_type="uses_model", dst_id=llama.id,
                          source=MemorySource.USER_EXPLICIT, at=t2)

    current = await traverse.neighbors(db_conn, desktop.id, rel_types=["uses_model"])
    assert [n["node"].name for n in current] == ["Llama 8B"]

    mid = datetime(2026, 3, 1, tzinfo=UTC)
    past = await traverse.neighbors(db_conn, desktop.id, rel_types=["uses_model"], as_of=mid)
    assert [n["node"].name for n in past] == ["Qwen 35B"]

    before = await traverse.neighbors(
        db_conn, desktop.id, rel_types=["uses_model"], as_of=datetime(2025, 1, 1, tzinfo=UTC)
    )
    assert before == []


async def test_contradiction_new_wins(db_conn: Any) -> None:
    x = await _node(db_conn, "e:x", "X")
    a = await _node(db_conn, "e:a", "A")
    b = await _node(db_conn, "e:b", "B")
    await writer.set_fact(db_conn, src_id=x.id, rel_type="is", dst_id=a.id,
                          source=MemorySource.INFERENCE)
    _, outcome = await writer.set_fact(db_conn, src_id=x.id, rel_type="is", dst_id=b.id,
                                       source=MemorySource.SYSTEM_OBSERVATION)
    assert outcome is not None and outcome.winner == "new" and outcome.resolution == "new_wins"
    current = await traverse.neighbors(db_conn, x.id, rel_types=["is"])
    assert [n["node"].name for n in current] == ["B"]  # stronger source became current
    assert await db_conn.fetchval(
        "SELECT count(*) FROM contradiction WHERE subject_type='graph_edge' AND resolution='new_wins'"
    ) == 1


async def test_contradiction_old_wins_preserves_current(db_conn: Any) -> None:
    x = await _node(db_conn, "e:x2", "X2")
    a = await _node(db_conn, "e:a2", "A2")
    b = await _node(db_conn, "e:b2", "B2")
    await writer.set_fact(db_conn, src_id=x.id, rel_type="is", dst_id=a.id,
                          source=MemorySource.SYSTEM_OBSERVATION)
    _, outcome = await writer.set_fact(db_conn, src_id=x.id, rel_type="is", dst_id=b.id,
                                       source=MemorySource.INFERENCE)
    assert outcome is not None and outcome.winner == "old" and outcome.resolution == "old_wins"
    current = await traverse.neighbors(db_conn, x.id, rel_types=["is"])
    assert [n["node"].name for n in current] == ["A2"]  # weaker new claim never displaced it
    # exactly one current edge (no two live values — fix M3)
    assert await db_conn.fetchval(
        "SELECT count(*) FROM graph_edge WHERE src_id=$1 AND rel_type='is' AND valid_until IS NULL", x.id
    ) == 1


async def test_relate_is_idempotent(db_conn: Any) -> None:
    x = await _node(db_conn, "e:x3", "X3")
    y = await _node(db_conn, "e:y3", "Y3")
    e1 = await writer.relate(db_conn, src_id=x.id, dst_id=y.id, rel_type="contains",
                             source=MemorySource.USER_EXPLICIT)
    e2 = await writer.relate(db_conn, src_id=x.id, dst_id=y.id, rel_type="contains",
                             source=MemorySource.USER_EXPLICIT)
    assert e1.id == e2.id


# ── contradiction verify-loop (§20/§25): open on a priority guess, close by re-observation ──

async def test_priority_conflict_on_observable_slot_opens_then_verifies(db_conn: Any) -> None:
    ollama = await _node(db_conn, "e:ov", "Ollama")
    v1 = await _node(db_conn, "e:v1", "0.32")
    v2 = await _node(db_conn, "e:v2", "0.33")
    v3 = await _node(db_conn, "e:v3", "0.34")
    # the machine established the slot; then a user CLAIMS a different version
    await writer.set_fact(db_conn, src_id=ollama.id, rel_type="has_version", dst_id=v1.id,
                          source=MemorySource.SYSTEM_OBSERVATION)
    await writer.set_fact(db_conn, src_id=ollama.id, rel_type="has_version", dst_id=v2.id,
                          source=MemorySource.USER_EXPLICIT)
    # priority kept the observation, but the conflict is OPEN — verify against the machine, don't guess
    assert await db_conn.fetchval("SELECT count(*) FROM contradiction WHERE status='open'") == 1

    # the machine is re-observed → the open contradiction is SETTLED BY VERIFICATION (§20 'then verify')
    await writer.set_fact(db_conn, src_id=ollama.id, rel_type="has_version", dst_id=v3.id,
                          source=MemorySource.SYSTEM_OBSERVATION)
    assert await db_conn.fetchval("SELECT count(*) FROM contradiction WHERE status='open'") == 0
    assert await db_conn.fetchval(
        "SELECT count(*) FROM contradiction WHERE resolved_by='verification'") >= 1


async def test_observation_driven_conflict_resolves_by_verification_immediately(db_conn: Any) -> None:
    gpu = await _node(db_conn, "e:gpu", "GPU")
    guess = await _node(db_conn, "e:guess", "guessed")
    actual = await _node(db_conn, "e:actual", "actual")
    await writer.set_fact(db_conn, src_id=gpu.id, rel_type="is", dst_id=guess.id,
                          source=MemorySource.INFERENCE)
    await writer.set_fact(db_conn, src_id=gpu.id, rel_type="is", dst_id=actual.id,
                          source=MemorySource.SYSTEM_OBSERVATION)  # a live look settles it now
    row = await db_conn.fetchrow(
        "SELECT status, resolved_by FROM contradiction WHERE subject_type='graph_edge' "
        "ORDER BY created_at DESC LIMIT 1")
    assert row["status"] == "resolved" and row["resolved_by"] == "verification"


async def test_non_observable_conflict_stays_evidence_priority(db_conn: Any) -> None:
    topic = await _node(db_conn, "e:np", "topic")
    a = await _node(db_conn, "e:p1", "one")
    b = await _node(db_conn, "e:p2", "two")
    await writer.set_fact(db_conn, src_id=topic.id, rel_type="is", dst_id=a.id,
                          source=MemorySource.CONVERSATION)
    await writer.set_fact(db_conn, src_id=topic.id, rel_type="is", dst_id=b.id,
                          source=MemorySource.INFERENCE)  # neither side is a live look
    row = await db_conn.fetchrow(
        "SELECT status, resolved_by FROM contradiction WHERE subject_type='graph_edge' "
        "ORDER BY created_at DESC LIMIT 1")
    assert row["status"] == "resolved" and row["resolved_by"] == "evidence_priority"


# ── multi-source provenance + corroboration (§6/§48) ──

async def test_multi_source_corroboration_raises_confidence(db_conn: Any) -> None:
    x = await _node(db_conn, "e:corr", "X")
    a = await _node(db_conn, "e:corra", "A")
    # a weak claim from one source
    e1 = await writer.relate(db_conn, src_id=x.id, dst_id=a.id, rel_type="uses",
                             source=MemorySource.INFERENCE, confidence=0.4)
    base = e1.confidence
    # the SAME source repeating adds no confidence (mere repetition) and no duplicate evidence row
    e2 = await writer.relate(db_conn, src_id=x.id, dst_id=a.id, rel_type="uses",
                             source=MemorySource.INFERENCE, confidence=0.4)
    assert e2.confidence == base
    # an INDEPENDENT source corroborating RAISES confidence (§6/§48)
    e3 = await writer.relate(db_conn, src_id=x.id, dst_id=a.id, rel_type="uses",
                             source=MemorySource.SYSTEM_OBSERVATION, confidence=0.9)
    assert e3.confidence > base and e3.confidence < 1.0  # strengthened, never proven

    sources = await db_conn.fetch(
        "SELECT DISTINCT source::text AS s FROM graph_evidence WHERE edge_id=$1 ORDER BY s", e1.id)
    assert [r["s"] for r in sources] == ["inference", "system_observation"]  # both attest, once each


async def test_new_node_and_edge_record_first_evidence(db_conn: Any) -> None:
    x = await _node(db_conn, "e:ev", "X")
    assert await db_conn.fetchval("SELECT count(*) FROM graph_evidence WHERE node_id=$1", x.id) == 1
    a = await _node(db_conn, "e:eva", "A")
    edge = await writer.relate(db_conn, src_id=x.id, dst_id=a.id, rel_type="runs",
                               source=MemorySource.USER_EXPLICIT)
    assert await db_conn.fetchval("SELECT count(*) FROM graph_evidence WHERE edge_id=$1", edge.id) == 1
