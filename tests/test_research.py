"""Architecture review · Increment 6 — the research faculty (§9): gap → web → understand → store.

Closes the online-learning loop and revives the dead learning_queue.resolve() read-half: a pending gap
is researched, the model distills an answer, and it's stored with provenance — never runs offline,
bounded by budget. web + connectivity are injected so CI never touches the network.
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.learning import queue, research
from sali.provider.base import ChatResult
from sali.provider.fake import FakeModelProvider

pytestmark = pytest.mark.db

_RESULTS = [{"title": "SVC docs", "url": "https://docs.example/svc", "domain": "docs.example",
             "snippet": "SVC needs PORT set before it starts."}]


async def _online(**_: Any) -> bool:
    return True


async def _offline(**_: Any) -> bool:
    return False


async def _search(_: str) -> list[dict[str, str]]:
    return _RESULTS


async def _no_results(_: str) -> list[dict[str, str]]:
    return []


def _answering(text: str) -> FakeModelProvider:
    return FakeModelProvider(responses=[ChatResult(text, None, [], 3, 3, "fake")])


async def test_research_stores_a_provenance_tagged_memory_and_resolves(live_pool: Any) -> None:
    async with live_pool.acquire() as conn:
        await queue.enqueue(conn, kind="investigate", subject="how do I configure SVC")

    learned = await research.research_pass(
        live_pool, _answering("Set the PORT env var before starting SVC."),
        online=_online, search=_search)

    assert learned == 1
    async with live_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT content, source, needs_grounding, structured FROM memory "
            "WHERE structured->>'kind'='researched' AND valid_until IS NULL")
        assert row is not None
        assert row["source"] == "external_source" and row["needs_grounding"] is True
        assert "PORT" in row["content"]
        assert row["structured"]["source_url"] == "https://docs.example/svc"  # provenance
        assert await queue.count_pending(conn) == 0  # the gap is resolved


async def test_offline_is_a_noop_and_leaves_the_gap_pending(live_pool: Any) -> None:
    async with live_pool.acquire() as conn:
        await queue.enqueue(conn, kind="investigate", subject="offline question")
    learned = await research.research_pass(live_pool, _answering("x"), online=_offline, search=_search)
    assert learned == 0
    async with live_pool.acquire() as conn:
        assert await queue.count_pending(conn) == 1  # never claim to have researched while offline


async def test_no_web_results_leaves_the_gap_pending(live_pool: Any) -> None:
    async with live_pool.acquire() as conn:
        await queue.enqueue(conn, kind="investigate", subject="rate limited question")
    learned = await research.research_pass(live_pool, _answering("x"), online=_online, search=_no_results)
    assert learned == 0
    async with live_pool.acquire() as conn:
        assert await queue.count_pending(conn) == 1  # web unavailable -> retry later, not resolved


async def test_inconclusive_research_resolves_without_storing(live_pool: Any) -> None:
    async with live_pool.acquire() as conn:
        await queue.enqueue(conn, kind="investigate", subject="unanswerable question")
    learned = await research.research_pass(live_pool, _answering("none"), online=_online, search=_search)
    assert learned == 0
    async with live_pool.acquire() as conn:
        assert await queue.count_pending(conn) == 0  # resolved so it isn't retried forever
        assert await conn.fetchval(
            "SELECT count(*) FROM memory WHERE structured->>'kind'='researched'") == 0  # nothing stored
