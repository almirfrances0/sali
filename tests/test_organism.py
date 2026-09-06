"""Persistent-organism additions — tests.

Covers the three new modules (OpenLoopStore, CuriosityStore, CommunicationDecisionEngine)
and their integration with the existing InitiativeEngine + ProactiveLoop.

Live-DB tests use the `live_pool` fixture (a scratch schema per test). Pure logic tests use
in-memory fake pools to keep the run fast.
"""

from __future__ import annotations

import pytest

from sali.events.communication_decision import CommunicationDecisionEngine, Decision


# ── OpenLoopStore ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_open_loop_open_or_reinforce_dedups_by_title(live_pool) -> None:
    """Two 'noticed X' calls with the same title should reinforce ONE row (bump priority,
    move last_touched_at), not create duplicates."""
    from sali.tasks.open_loops import OpenLoopStore

    store = OpenLoopStore(live_pool)
    id1 = await store.open_or_reinforce(title="Investigate API latency", priority=0.4)
    id2 = await store.open_or_reinforce(title="Investigate API latency", priority=0.4)
    assert id1 == id2, "same title should dedup"

    loops = await store.open_loops(limit=10)
    matching = [l for l in loops if l["title"] == "Investigate API latency"]
    assert len(matching) == 1
    # Priority nudged up by 0.05 on the reinforcement (bounded at 1.0)
    assert matching[0]["priority"] > 0.4


@pytest.mark.asyncio
async def test_open_loop_touch_and_resolve(live_pool) -> None:
    """touch() must bump priority; resolve() sets status + resolution."""
    from sali.tasks.open_loops import OpenLoopStore

    store = OpenLoopStore(live_pool)
    lid = await store.open_or_reinforce(title="Verify the WS reconnect fix", priority=0.5)
    assert await store.touch(lid, priority_bump=0.1) is True

    loops = await store.open_loops(limit=10)
    match = next(l for l in loops if l["id"] == lid)
    assert match["priority"] >= 0.6  # 0.5 + 0.1 bump

    assert await store.resolve(lid, resolution="Verified via test rerun; fix holds.") is True

    # No longer in open list
    assert lid not in [l["id"] for l in await store.open_loops(limit=10)]


@pytest.mark.asyncio
async def test_open_loop_as_initiative_candidates_filters_by_priority(live_pool) -> None:
    """Only loops at priority >= 0.5 should feed the InitiativeEngine (low-priority noticed
    items must not dominate the budget)."""
    from sali.tasks.open_loops import OpenLoopStore

    store = OpenLoopStore(live_pool)
    await store.open_or_reinforce(title="High priority loop", priority=0.7)
    await store.open_or_reinforce(title="Low priority loop", priority=0.2)

    candidates = await store.as_initiative_candidates(limit=10)
    titles = [c["title"] for c in candidates]
    assert "High priority loop" in titles
    assert "Low priority loop" not in titles


# ── CuriosityStore ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_curiosity_encounter_dedups_by_subject_slug(live_pool) -> None:
    """Repeated encounters of the same subject reinforce ONE row (times_encountered rises;
    priority bumps up, capped at 1.0)."""
    from sali.learning.curiosity import CuriosityStore

    store = CuriosityStore(live_pool)
    id1 = await store.encounter(subject="Rust Tokio Async", statement="Keep seeing rust-tokio",
                                priority=0.4)
    id2 = await store.encounter(subject="Rust Tokio Async", statement="Almir mentioned again",
                                priority=0.5)
    assert id1 == id2, "same subject slug should dedup"

    rows = await store.open_curiosities(limit=10)
    match = next(r for r in rows if r["id"] == id1)
    assert match["times_encountered"] == 2
    assert match["priority"] > 0.4  # bumped on reinforcement


@pytest.mark.asyncio
async def test_curiosity_learn_appends_discoveries(live_pool) -> None:
    from sali.learning.curiosity import CuriosityStore

    store = CuriosityStore(live_pool)
    cid = await store.encounter(subject="pgvector HNSW",
                                statement="Encountered a pgvector index type I don't know")
    assert await store.learn(cid, note="HNSW is a graph-based index, better for high recall",
                             new_understanding="HNSW indexes trade build time for query recall") is True

    rows = await store.open_curiosities(limit=10)
    match = next(r for r in rows if r["id"] == cid)
    assert len(match["discoveries"]) == 1
    assert "HNSW is a graph-based" in match["discoveries"][0]["note"]
    assert "trade build time" in match["current_understanding"]


@pytest.mark.asyncio
async def test_curiosity_only_high_priority_becomes_initiative_candidate(live_pool) -> None:
    """A curiosity below priority 0.6 should NOT feed the InitiativeEngine — idle-time research
    must be a legitimately strong knowledge gap, not just noise."""
    from sali.learning.curiosity import CuriosityStore

    store = CuriosityStore(live_pool)
    await store.encounter(subject="Strong Curiosity", statement="high", priority=0.7)
    await store.encounter(subject="Weak Curiosity", statement="low", priority=0.3)

    cands = await store.as_initiative_candidates(limit=10)
    subs = [c["subject"] for c in cands]
    assert "strong-curiosity" in subs
    assert "weak-curiosity" not in subs


# ── CommunicationDecisionEngine ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_comms_gate_sends_when_no_reason_to_suppress(live_pool) -> None:
    gate = CommunicationDecisionEngine(live_pool)
    decision = await gate.evaluate(kind="observation", subject_ref="port_9999",
                                   relevance=0.7, user_focused=False,
                                   message="A new service opened on port 9999.")
    assert decision.send is True
    assert decision.reason_codes == []


@pytest.mark.asyncio
async def test_comms_gate_suppresses_when_too_frequent(live_pool) -> None:
    """Two messages of the same kind within the frequency window: second is suppressed with
    the 'too_frequent' reason code."""
    gate = CommunicationDecisionEngine(live_pool)
    d1 = await gate.evaluate(kind="greeting", relevance=0.6, message="Hey Sir")
    assert d1.send is True
    d2 = await gate.evaluate(kind="greeting", relevance=0.6, message="Good morning")
    assert d2.send is False
    assert "too_frequent" in d2.reason_codes


@pytest.mark.asyncio
async def test_comms_gate_suppresses_low_relevance_or_focused_user(live_pool) -> None:
    gate = CommunicationDecisionEngine(live_pool)
    d = await gate.evaluate(kind="observation", subject_ref="s1", relevance=0.1)
    assert d.send is False
    assert "low_relevance" in d.reason_codes

    d2 = await gate.evaluate(kind="social_checkin", subject_ref="s2", relevance=0.5,
                             user_focused=True)
    assert d2.send is False
    assert "user_focused" in d2.reason_codes


@pytest.mark.asyncio
async def test_comms_gate_records_every_decision_as_audit_row(live_pool) -> None:
    """Every evaluate() call — sent or suppressed — must land in sali.proactive_decision so the
    §46 aggregator can learn from it."""
    gate = CommunicationDecisionEngine(live_pool)
    await gate.evaluate(kind="observation", subject_ref="unique-subj-audit",
                        relevance=0.05, message="ignored")

    async with live_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT decision, reason_codes FROM sali.proactive_decision "
            "WHERE subject_ref = 'unique-subj-audit' ORDER BY decided_at DESC LIMIT 1")
    assert row is not None
    assert row["decision"] == "suppressed"
    assert "low_relevance" in row["reason_codes"]


# ── InitiativeEngine composition (open loops + curiosities feed the initiative loop) ─────────


@pytest.mark.asyncio
async def test_initiative_engine_reads_open_loops_and_curiosities(live_pool) -> None:
    """A high-priority open loop + a high-priority curiosity should become initiative candidates
    the next time InitiativeEngine.generate_candidates runs."""
    from sali.learning.curiosity import CuriosityStore
    from sali.runtime.initiative import InitiativeEngine
    from sali.tasks.open_loops import OpenLoopStore

    await OpenLoopStore(live_pool).open_or_reinforce(
        title="Follow up on API latency dip", priority=0.75)
    await CuriosityStore(live_pool).encounter(
        subject="HNSW index tradeoffs", statement="Almir picked this in a chat",
        priority=0.7)

    engine = InitiativeEngine(live_pool)
    touched = await engine.generate_candidates()
    assert len(touched) >= 2, f"expected at least the loop+curiosity to become candidates, got {len(touched)}"

    # Verify by SOURCE — the initiative store should now hold rows keyed by our new sources.
    async with live_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT source, title FROM sali.initiative "
            "WHERE source IN ('open_loop', 'curiosity') ORDER BY source")
    sources = {r["source"] for r in rows}
    assert "open_loop" in sources
    assert "curiosity" in sources
