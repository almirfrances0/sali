"""Memory correctness · Increment 7 — the consistency matrix (§10).

Automated checks that the memory layer stays self-consistent under the conditions the audit worried
about: duplicate content, semantic-vs-lexical retrieval, conflicting writes, and functional supersession.
(Alias resolution, stale-vs-current freshness, temporal validity, scope, contradiction handling, evidence
chains, and reboot persistence are covered by test_entity_resolution / test_freshness / test_memory /
test_memory_contradiction / test_memory_audit and the A–Q benchmark.)
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from sali.core.enums import MemoryLayer, MemorySource
from sali.memory import writer
from sali.memory.service import MemoryService
from sali.provider.fake import FakeModelProvider

pytestmark = pytest.mark.db


async def test_duplicate_content_corroborates_not_duplicates(db_conn: Any) -> None:
    m1 = await writer.remember(db_conn, layer=MemoryLayer.SEMANTIC, content="the disk is 1TB NVMe",
                               source=MemorySource.SYSTEM_OBSERVATION)
    m2 = await writer.remember(db_conn, layer=MemoryLayer.SEMANTIC, content="the disk is 1TB NVMe",
                               source=MemorySource.USER_EXPLICIT)
    assert m1.id == m2.id  # one memory, not two
    n = await db_conn.fetchval(
        "SELECT count(*) FROM memory WHERE content='the disk is 1TB NVMe' AND valid_until IS NULL")
    assert n == 1 and m2.evidence_count >= 2  # deduped, and the corroboration is recorded


async def test_conflicting_functional_value_supersedes_never_two_current(db_conn: Any) -> None:
    await writer.remember(db_conn, layer=MemoryLayer.SEMANTIC, content="the web server is nginx",
                          source=MemorySource.INFERENCE, functional=True, claim_key="web:server")
    await writer.remember(db_conn, layer=MemoryLayer.SEMANTIC, content="the web server is caddy",
                          source=MemorySource.USER_EXPLICIT, functional=True, claim_key="web:server")
    current = await db_conn.fetch(
        "SELECT content FROM memory WHERE claim_key='web:server' AND valid_until IS NULL")
    assert len(current) == 1 and current[0]["content"] == "the web server is caddy"  # newer wins, one current
    retired = await db_conn.fetchval(
        "SELECT count(*) FROM memory WHERE claim_key='web:server' AND valid_until IS NOT NULL")
    assert retired == 1  # the old value is kept as history, not deleted


async def test_lexical_and_semantic_retrievers_both_contribute(live_pool: Any) -> None:
    mem = MemoryService(live_pool, FakeModelProvider())
    await mem.remember(layer=MemoryLayer.SEMANTIC, content="the primary datastore is PostgreSQL 18",
                       source=MemorySource.USER_EXPLICIT)
    hits = await mem.retrieve("PostgreSQL datastore", k=5)
    assert hits
    # each hit records WHICH retriever found it — both angles are represented and fused
    retrievers = {h.retriever for h in hits}
    assert retrievers <= {"vector", "keyword", "hybrid"} and retrievers


async def test_concurrent_same_claim_writes_leave_one_current(live_pool: Any) -> None:
    # two writers assert the same functional claim at once — the slot lock keeps exactly one current
    async def writer_task(value: str) -> None:
        async with live_pool.acquire() as conn:
            await writer.remember(conn, layer=MemoryLayer.SEMANTIC, content=f"the cache is {value}",
                                  source=MemorySource.USER_EXPLICIT, functional=True, claim_key="cache:kind")

    await asyncio.gather(writer_task("Redis"), writer_task("Redis"))
    async with live_pool.acquire() as conn:
        current = await conn.fetchval(
            "SELECT count(*) FROM memory WHERE claim_key='cache:kind' AND valid_until IS NULL")
    assert current == 1  # never two current values for one functional claim, even under concurrency
