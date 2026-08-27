"""The memory evaluation harness (§67/§68): the benchmark passes on the real memory system, and it
never persists its scenario."""

from __future__ import annotations

from typing import Any

import pytest

from sali.provider.fake import FakeModelProvider
from sali.retrieval.benchmark import CASES, run_benchmark

pytestmark = pytest.mark.db


async def test_memory_benchmark_passes_every_dimension(live_pool: Any) -> None:
    card = await run_benchmark(live_pool, FakeModelProvider())
    failed = [f"{name} ({dim})" for name, dim, ok in card.results if not ok]
    assert not failed, f"benchmark regressions: {failed}"
    assert card.passed == card.total == len(CASES)
    # every §67 dimension is covered and green
    for dim, (passed, total) in card.by_dimension.items():
        assert passed == total, dim


async def test_benchmark_leaves_no_trace(live_pool: Any) -> None:
    # It runs in a rolled-back transaction — the store is identical before and after.
    async def _counts() -> tuple[int, int, int]:
        async with live_pool.acquire() as c:
            return (
                await c.fetchval("SELECT count(*) FROM memory"),
                await c.fetchval("SELECT count(*) FROM graph_node"),
                await c.fetchval("SELECT count(*) FROM contradiction"),
            )

    before = await _counts()
    await run_benchmark(live_pool, FakeModelProvider())
    assert await _counts() == before  # nothing seeded by the benchmark survived
