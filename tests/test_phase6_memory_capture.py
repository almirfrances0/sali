"""PHASE 6 — reliability matrix · memory capture & the person graph.

The relationship extractor feeds the person graph (person:almir --rel--> person:<name>); the harden pass
found it minted GARBAGE nodes ('This'/'He'/'God'/'Google') as Almir's kin on the most natural phrasing.
The open-loop capture filed Sali's own chain-of-thought ("First, I need to understand…") as a durable
follow-up. This locks both fixes ([H]) plus the intro false-negatives that were also repaired.
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.runtime.loop import (
    _OPEN_LOOP_FILLER_RE,
    _OPEN_LOOP_RE,
    _deterministic_fact,
    _durable_signal,
    _extract_relationship,
)


# ---- deterministic capture no longer depends on the flaky model gate -----------------------------

def test_deterministic_fact_extracts_the_wife_case() -> None:
    # The Q2_K KEEP/SKIP gate captured 0 memories in 3 days; a clear fact must persist WITHOUT it.
    assert _deterministic_fact("this is my wife, her name is Faith Evans") == "Your wife, her name is Faith Evans"
    assert _deterministic_fact("my main project is TanzHost") == "Your main project is TanzHost"


def test_deterministic_fact_ignores_non_fact_chatter() -> None:
    for s in ("can you help me later today", "what's the weather", "thanks, that worked"):
        assert _deterministic_fact(s) is None, s


# ---- relationship extraction never mints garbage nodes -------------------------------------------

def test_demonstrative_intro_resolves_to_the_real_name() -> None:  # [H] was ('This', married_to)
    assert _extract_relationship("This is my wife Faith") == ("Faith", "married_to")
    assert _extract_relationship("this is my wife Faith") == ("Faith", "married_to")


def test_capitalized_nonnames_mint_nothing() -> None:  # [H] no person:he / person:god / person:google
    for s in ("He is my brother", "She is my sister", "God is my father",
              "Nobody is my friend", "They are my team"):
        assert _extract_relationship(s) is None, s


def test_real_name_relationship_still_extracted() -> None:
    assert _extract_relationship("Faith is my wife") == ("Faith", "married_to")


# ---- the intro false-negatives the harden pass also fixed ----------------------------------------

def test_bare_name_after_rel_captured() -> None:
    assert _durable_signal("my brother Tom called") is True
    assert _extract_relationship("my brother Tom called") == ("Tom", "sibling_of")


def test_comma_appositive_captured() -> None:
    assert _extract_relationship("my wife, Faith, is here") == ("Faith", "married_to")


# ---- open-loop capture rejects internal planning narration ---------------------------------------

def _would_capture(reply: str) -> str | bool:
    """Mirror of loop._capture_open_loop's decision, so we test exactly what it would file."""
    m = _OPEN_LOOP_RE.search(reply)
    if not m:
        return False
    idx = m.start()
    left = reply.rfind(".", 0, idx)
    right = reply.find(".", idx)
    if left < 0:
        left = -1
    sentence = reply[left + 1: right if right >= 0 else len(reply)].strip()
    title = " ".join(sentence.split())[:120]
    if len(title) < 12 or _OPEN_LOOP_FILLER_RE.search(title):
        return False
    return title


def test_chain_of_thought_filler_not_captured() -> None:  # [H] the junk-row source
    for r in ("First, I need to understand what's going on.",
              "First, I need to understand the current situation.",
              "Okay, let me start by figuring out what is going on here."):
        assert _would_capture(r) is False, r


def test_genuine_open_loop_still_captured() -> None:
    for r in ("I'll look into the tanzahost billing discrepancy tomorrow.",
              "I need to investigate why the nginx config keeps reverting.",
              "Still need to follow up on the DNS migration for the client."):
        assert _would_capture(r), r


# ---- end-to-end: a stated fact lands as memory AND a graph edge, with the model gate FAILING ------

@pytest.mark.db
async def test_capture_durable_persists_fact_and_mints_graph_edge_without_the_model_gate(live_pool: Any) -> None:
    """REGRESSION (Phase-1 audit). Two defects made the whole user-fact pipeline a facade:
      1. persistence depended on a Q2_K KEEP:/SKIP call that captured 0 memories in 3 days;
      2. the relationship graph was read off `self._memory_sink._graph` (never set -> always None),
         so `person:almir --married_to--> person:<name>` was never minted.
    Here the provider deliberately returns SKIP, and the wife-case must STILL land as a USER_EXPLICIT
    memory AND a real married_to edge -- proving capture no longer depends on the model gate and the
    graph handle points at the loop's real GraphService."""
    from sali.core.enums import MemorySource
    from sali.graph import traverse
    from sali.graph.service import GraphService
    from sali.provider.base import ChatMessage, ChatResult
    from sali.runtime.loop import AgentLoop

    captured: dict[str, Any] = {}

    class _Sink:
        async def remember(self, content: str, **kwargs: Any) -> Any:
            captured["content"] = content
            captured.update(kwargs)
            return None

    class _Provider:  # the model GATE fails (SKIP) -- the deterministic net must carry the fact
        async def chat(self, messages: list[ChatMessage], **_: Any) -> ChatResult:
            return ChatResult(model="fake", content="SKIP", tokens_in=1, tokens_out=1,
                              thinking="", tool_calls=[])

    class _Journal:
        async def event(self, *a: Any, **k: Any) -> None:
            pass

    loop = object.__new__(AgentLoop)
    loop.provider = _Provider()
    loop._memory_sink = _Sink()
    loop._graph = GraphService(live_pool)

    await loop._capture_durable("this is my wife, her name is Faith Evans", "Got it.", _Journal())

    # (1) deterministic safety net: the fact persisted as USER_EXPLICIT despite the SKIP gate
    assert captured.get("source") is MemorySource.USER_EXPLICIT
    assert "Faith Evans" in captured.get("content", ""), captured

    # (2) the wiring fix: person:almir --married_to--> person:faith_evans exists in the graph
    async with live_pool.acquire() as conn:
        almir = await conn.fetchrow(
            "SELECT id FROM graph_node WHERE canonical_key='person:almir' AND valid_until IS NULL")
        assert almir is not None, "person:almir node was not minted"
        neighbors = await traverse.neighbors(conn, almir["id"], rel_types=["married_to"])
    names = [n["node"].name for n in neighbors]
    assert "Faith Evans" in names, f"expected a married_to edge to Faith Evans, got {names}"
