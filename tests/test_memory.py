"""Memory write/retrieve integration (spec test 1) + the working-memory guarantee."""

from __future__ import annotations

from typing import Any

import pytest

from sali.core.enums import MemoryLayer, MemorySource
from sali.core.errors import ProviderError, SaliError
from sali.memory import writer
from sali.memory.embed_worker import DOC_PREFIX, embed_pending_conn
from sali.memory.retriever import retrieve_keyword, retrieve_vector
from sali.memory.service import MemoryService
from sali.provider.fake import FakeModelProvider

pytestmark = pytest.mark.db


class _EmbedderDown(FakeModelProvider):
    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise ProviderError("embedder offline")


async def test_retrieve_degrades_to_keyword_when_the_embedder_is_down(live_pool: Any) -> None:
    # §53: the embedder is a SEPARATE CPU model — if it's down, recall must degrade to lexical, not
    # fail the whole turn. remember() still works (its embedding just stays pending).
    mem = MemoryService(live_pool, _EmbedderDown())
    await mem.remember(layer=MemoryLayer.SEMANTIC, content="Sali runs on Kali Linux",
                       source=MemorySource.USER_EXPLICIT)
    hits = await mem.retrieve("Kali Linux")  # must NOT raise
    assert any("Kali" in h.memory.content for h in hits)
    assert all(h.retriever == "keyword" for h in hits)  # no vector hits when the embedder is down


async def test_remember_then_keyword_retrieve(db_conn: Any) -> None:
    m = await writer.remember(
        db_conn,
        layer=MemoryLayer.SEMANTIC,
        content="Almir uses Kali Linux as his primary OS",
        source=MemorySource.USER_EXPLICIT,
    )
    assert m.confidence > 0.5
    rows = await retrieve_keyword(db_conn, "Kali", 10)
    assert any(r["id"] == m.id for r in rows)


async def test_working_memory_cannot_be_remembered(db_conn: Any) -> None:
    with pytest.raises(SaliError):
        await writer.remember(
            db_conn, layer=MemoryLayer.WORKING, content="scratch", source=MemorySource.INFERENCE
        )


async def test_observe_never_touches_memory(db_conn: Any) -> None:
    before = await db_conn.fetchval("SELECT count(*) FROM memory")
    await writer.observe(db_conn, kind="note", content="ephemeral", source=MemorySource.INFERENCE)
    after = await db_conn.fetchval("SELECT count(*) FROM memory")
    assert after == before
    assert await db_conn.fetchval(
        "SELECT count(*) FROM stm_observation WHERE content='ephemeral'"
    ) == 1


async def test_restatement_corroborates_not_duplicates(db_conn: Any) -> None:
    m1 = await writer.remember(
        db_conn,
        layer=MemoryLayer.SEMANTIC,
        content="Ollama is installed",
        source=MemorySource.SYSTEM_OBSERVATION,
    )
    m2 = await writer.remember(
        db_conn,
        layer=MemoryLayer.SEMANTIC,
        content="Ollama is installed",
        source=MemorySource.SYSTEM_OBSERVATION,
    )
    assert m2.id == m1.id
    assert m2.evidence_count == 2
    # §6 distinct-source rule: the SAME source re-stating a fact is tracked (evidence_count bumps) but
    # must NOT inflate confidence — otherwise Sali could self-reinforce a belief by simply repeating it.
    # A DIFFERENT source is what raises confidence (see the distinct-source path).
    assert m2.confidence == m1.confidence
    current = await db_conn.fetchval(
        "SELECT count(*) FROM memory WHERE content='Ollama is installed' AND valid_until IS NULL"
    )
    assert current == 1


async def test_embed_then_vector_retrieve(db_conn: Any) -> None:
    fake = FakeModelProvider(dim=768)
    content = "The RTX 4070 has 12GB of VRAM"
    m = await writer.remember(
        db_conn, layer=MemoryLayer.SYSTEM_ENV, content=content, source=MemorySource.SYSTEM_OBSERVATION
    )
    embedded = await embed_pending_conn(db_conn, fake, batch=32)
    assert embedded >= 1
    assert await db_conn.fetchval("SELECT embed_status FROM memory WHERE id=$1", m.id) == "done"

    # Query with the same document vector → cosine similarity ~1 → ranked first.
    doc_vec = fake._vector(DOC_PREFIX + content)  # noqa: SLF001 - deterministic test oracle
    rows = await retrieve_vector(db_conn, doc_vec, limit=5)
    assert rows and rows[0]["id"] == m.id
    assert rows[0]["similarity"] > 0.99


async def test_retrieval_excludes_superseded(db_conn: Any) -> None:
    m = await writer.remember(
        db_conn,
        layer=MemoryLayer.SEMANTIC,
        content="obsolete driver 550 is current",
        source=MemorySource.SYSTEM_OBSERVATION,
    )
    await db_conn.execute("UPDATE memory SET valid_until = now() WHERE id=$1", m.id)
    rows = await retrieve_keyword(db_conn, "obsolete driver", 10)
    assert all(r["id"] != m.id for r in rows)  # only current memories are retrieved


# ── free-text possible-conflict detection (§13/§46): surface, never auto-resolve ──

async def test_near_identical_semantic_memory_is_flagged_possible_conflict(db_conn: Any) -> None:
    from sali.memory.embed_worker import _note_possible_conflict
    from sali.memory.retriever import vector_literal

    m1 = await writer.remember(db_conn, layer=MemoryLayer.SEMANTIC,
                               content="Project X uses PostgreSQL", source=MemorySource.USER_EXPLICIT)
    m2 = await writer.remember(db_conn, layer=MemoryLayer.SEMANTIC,
                               content="Project X uses MySQL", source=MemorySource.USER_EXPLICIT)
    same = vector_literal([0.1] * 768)  # simulate two semantically-similar embeddings
    for mid in (m1.id, m2.id):
        await db_conn.execute(
            "UPDATE memory SET embedding=$1::vector, embed_status='done' WHERE id=$2", same, mid)

    await _note_possible_conflict(db_conn, m2.id, same)
    flagged = await db_conn.fetchval(
        "SELECT count(*) FROM event WHERE event_type='memory.possible_conflict' AND subject_id=$1", m2.id)
    assert flagged == 1  # the very-similar pair is surfaced for review...
    # ...but NEITHER memory was superseded or retired — no silent resolution (§46)
    assert await db_conn.fetchval(
        "SELECT count(*) FROM memory WHERE id=ANY($1) AND valid_until IS NULL", [m1.id, m2.id]) == 2


async def test_unrelated_semantic_memory_is_not_flagged(db_conn: Any) -> None:
    from sali.memory.embed_worker import _note_possible_conflict
    from sali.memory.retriever import vector_literal

    m1 = await writer.remember(db_conn, layer=MemoryLayer.SEMANTIC,
                               content="the office is in Berlin", source=MemorySource.USER_EXPLICIT)
    m2 = await writer.remember(db_conn, layer=MemoryLayer.SEMANTIC,
                               content="lunch is at noon", source=MemorySource.USER_EXPLICIT)
    v1 = [0.0] * 768
    v1[0] = 1.0
    v2 = [0.0] * 768
    v2[400] = 1.0  # orthogonal → low similarity
    await db_conn.execute("UPDATE memory SET embedding=$1::vector, embed_status='done' WHERE id=$2",
                          vector_literal(v1), m1.id)
    await db_conn.execute("UPDATE memory SET embedding=$1::vector, embed_status='done' WHERE id=$2",
                          vector_literal(v2), m2.id)

    await _note_possible_conflict(db_conn, m2.id, vector_literal(v2))
    assert await db_conn.fetchval(
        "SELECT count(*) FROM event WHERE event_type='memory.possible_conflict' AND subject_id=$1",
        m2.id) == 0  # unrelated facts are never flagged
