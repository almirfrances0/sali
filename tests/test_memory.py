"""Memory write/retrieve integration (spec test 1) + the working-memory guarantee."""

from __future__ import annotations

from typing import Any

import pytest

from sali.core.enums import MemoryLayer, MemorySource
from sali.core.errors import SaliError
from sali.memory import writer
from sali.memory.embed_worker import DOC_PREFIX, embed_pending_conn
from sali.memory.retriever import retrieve_keyword, retrieve_vector
from sali.provider.fake import FakeModelProvider

pytestmark = pytest.mark.db


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
    assert m2.confidence > m1.confidence  # corroboration raised it
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
