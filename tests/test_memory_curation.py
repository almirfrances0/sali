"""Memory self-correction / curation (§40/§42/§55/§56): verify + forget, reversible, history-safe."""

from __future__ import annotations

from typing import Any

import pytest

from sali.config.settings import Settings
from sali.core.clock import SystemClock
from sali.core.enums import MemoryLayer, MemorySource
from sali.memory.service import MemoryService
from sali.provider.fake import FakeModelProvider
from sali.runtime.loop import _MemorySink
from sali.tools.builtins.memory_tool import ForgetBelief, VerifyBelief
from sali.tools.context import ToolContext

pytestmark = pytest.mark.db


async def test_forget_retires_a_belief_but_keeps_its_history(live_pool: Any) -> None:
    mem = MemoryService(live_pool, FakeModelProvider())
    m = await mem.remember(layer=MemoryLayer.SEMANTIC, content="Project Y uses MongoDB",
                           source=MemorySource.CONVERSATION)
    await mem.embed_pending()

    result = await mem.forget_matching("Project Y MongoDB database", reason="Almir said it's Postgres")
    assert result["found"] and result["closed"] and result["forgot"] == "Project Y uses MongoDB"

    async with live_pool.acquire() as c:
        row = await c.fetchrow("SELECT valid_until FROM memory WHERE id=$1", m.id)
    assert row is not None and row["valid_until"] is not None  # retired from current, NOT hard-deleted


async def test_verify_regrounds_a_memory(live_pool: Any) -> None:
    mem = MemoryService(live_pool, FakeModelProvider())
    m = await mem.remember(layer=MemoryLayer.SEMANTIC, content="Docker is running on the box",
                           source=MemorySource.EXTERNAL_SOURCE, needs_grounding=True)
    await mem.embed_pending()

    res = await mem.verify_matching("Docker running status", verified=True)
    assert res["found"] and res["updated"]

    async with live_pool.acquire() as c:
        row = await c.fetchrow("SELECT needs_grounding FROM memory WHERE id=$1", m.id)
        evidence = await c.fetchval("SELECT count(*) FROM memory_evidence WHERE memory_id=$1", m.id)
    assert row["needs_grounding"] is False and evidence >= 1  # confirmed → grounded + evidence recorded


async def test_forget_of_the_unknown_forgets_nothing(live_pool: Any) -> None:
    mem = MemoryService(live_pool, FakeModelProvider())
    result = await mem.forget_matching("a belief that was never stored", reason="x")
    assert result == {"found": False}  # nothing matched → nothing retired, no invention


async def test_forget_and_verify_tools_end_to_end(live_pool: Any) -> None:
    mem = MemoryService(live_pool, FakeModelProvider())
    await mem.remember(layer=MemoryLayer.SEMANTIC, content="the staging server is 10.0.0.5",
                       source=MemorySource.CONVERSATION)
    await mem.embed_pending()
    ctx = ToolContext(settings=Settings(), clock=SystemClock(), memory=_MemorySink(mem))

    verified = await VerifyBelief().run({"belief": "staging server address 10.0.0.5", "verified": True}, ctx)
    assert verified.ok and verified.output["found"]

    forgotten = await ForgetBelief().run(
        {"belief": "staging server is 10.0.0.5", "reason": "it moved"}, ctx)
    assert forgotten.ok and forgotten.output["found"] and forgotten.output["closed"]


async def test_weak_inference_is_a_candidate_then_promotes(live_pool: Any) -> None:
    # §57 write-policy gate: a lone unverified INFERENCE is held as a candidate (needs_grounding),
    # and PROMOTES only once corroboration lifts it past the layer's confidence + evidence bar.
    from sali.memory import writer as w

    async with live_pool.acquire() as c:
        m = await w.remember(c, layer=MemoryLayer.SEMANTIC, content="the cache is probably Redis",
                             source=MemorySource.INFERENCE)
        assert m.needs_grounding is True  # gated — a weak conclusion isn't first-class memory yet

        # a stronger source corroborating the same content raises confidence + evidence → promoted
        promoted = await w.remember(c, layer=MemoryLayer.SEMANTIC, content="the cache is probably Redis",
                                    source=MemorySource.USER_EXPLICIT)
        assert promoted.needs_grounding is False and promoted.evidence_count >= 2


async def test_a_direct_statement_is_never_gated(live_pool: Any) -> None:
    from sali.memory import writer as w

    async with live_pool.acquire() as c:
        m = await w.remember(c, layer=MemoryLayer.SEMANTIC, content="Project X uses PostgreSQL",
                             source=MemorySource.USER_EXPLICIT)
    assert m.needs_grounding is False  # a user statement clears the bar immediately


async def test_web_fact_is_never_promoted_by_mere_restatement(live_pool: Any) -> None:
    # §11: a web/external fact's needs_grounding is a REALITY-grounding requirement — restating the
    # same web claim (even many times) must NOT strip the "unverified" flag; only an actual check
    # (reground / memory_verify) can. Otherwise Sali would trust the internet by repetition.
    from sali.memory import writer as w

    async with live_pool.acquire() as c:
        m = await w.remember(c, layer=MemoryLayer.SEMANTIC, content="Postgres 18 shipped async I/O",
                             source=MemorySource.EXTERNAL_SOURCE, needs_grounding=True)
        assert m.needs_grounding is True
        again = await w.remember(c, layer=MemoryLayer.SEMANTIC, content="Postgres 18 shipped async I/O",
                                 source=MemorySource.EXTERNAL_SOURCE)
        # corroboration raised confidence + evidence, but the reality-grounding flag stands
        assert again.needs_grounding is True and again.evidence_count >= 2


async def test_a_candidate_needs_independent_sources_to_promote(live_pool: Any) -> None:
    # §6/§57: the promotion bar counts DISTINCT sources, not raw restatements — so Sali restating its
    # OWN inference can't self-promote a candidate. Only a second, independent source does.
    from sali.memory import writer as w

    async with live_pool.acquire() as c:
        m = await w.remember(c, layer=MemoryLayer.SEMANTIC, content="the queue is probably RabbitMQ",
                             source=MemorySource.INFERENCE)
        assert m.needs_grounding is True
        # same source again — confidence/evidence tick up, but still ONE independent source → candidate
        same = await w.remember(c, layer=MemoryLayer.SEMANTIC, content="the queue is probably RabbitMQ",
                                source=MemorySource.INFERENCE)
        assert same.needs_grounding is True
        # now a genuinely independent source attests it → promoted
        promoted = await w.remember(c, layer=MemoryLayer.SEMANTIC, content="the queue is probably RabbitMQ",
                                    source=MemorySource.USER_EXPLICIT)
        assert promoted.needs_grounding is False


async def test_a_secret_is_redacted_before_it_reaches_the_store(live_pool: Any) -> None:
    # §27: a raw secret Almir mentions must never land in Postgres (and thus a backup) verbatim.
    from sali.memory import writer as w

    async with live_pool.acquire() as c:
        m = await w.remember(c, layer=MemoryLayer.SEMANTIC,
                             content="the VPS deploy url is ssh://root:hunter2please@74.207.227.95/",
                             source=MemorySource.USER_EXPLICIT)
        stored = await c.fetchval("SELECT content FROM memory WHERE id=$1", m.id)
    assert "hunter2please" not in stored and "[redacted]" in stored
