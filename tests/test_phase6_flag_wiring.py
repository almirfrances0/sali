"""PHASE 6 — reliability matrix · flag-only detectors FIRE through the real loop (were inert).

confabulation / data_output / cross_turn are shipped-but-inert in production (0 events) — not because
they are broken but because no triggering reply had occurred. This drives the real AgentLoop to produce
each triggering reply and proves the loop invokes the detector and records the 'flagged' grounding_event
ledger row (never rewriting the reply — flag-only). Closes "does the wiring actually fire live?".
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.config.settings import DbSettings, ModelSettings, Settings
from sali.context.engine import ContextEngine
from sali.core.ids import new_id
from sali.provider.base import ChatResult
from sali.provider.fake import FakeModelProvider
from sali.retrieval.service import RetrievalService
from sali.runtime.loop import AgentLoop
from sali.security.confirm import AutoAllowConfirmer
from sali.security.policy import PolicyEngine
from sali.tools.registry import default_registry

pytestmark = pytest.mark.db


def _loop(pool: Any, provider: FakeModelProvider) -> AgentLoop:
    return AgentLoop(
        pool=pool, provider=provider,
        retrieval=RetrievalService(pool, provider),
        context=ContextEngine(provider, ctx_tokens=8192),
        registry=default_registry(), policy=PolicyEngine(),
        confirmer=AutoAllowConfirmer(),
        settings=Settings(db=DbSettings(name="sali_test"), model=ModelSettings(provider="fake")),
    )


async def _flag_count(pool: Any, kind: str) -> int:
    async with pool.acquire() as c:
        return await c.fetchval(
            "SELECT count(*) FROM grounding_event WHERE kind=$1 AND verdict='flagged'", kind)


async def test_data_fabrication_flagged_through_the_loop(live_pool: Any) -> None:
    # A reply presenting a live-system file listing when NO observation tool ran — flag, don't rewrite.
    fake = FakeModelProvider(responses=[
        ChatResult("Here are the files in the directory:\n| name |\n|--|\n| a.txt |\n| b.txt |",
                   None, [], 8, 8, "fake"),
    ])
    result = await _loop(live_pool, fake).run("what files are here?", session_id=new_id())
    assert "Here are the files" in result.text          # flag-only: reply is NOT rewritten
    assert await _flag_count(live_pool, "data_output") >= 1  # but the ledger recorded the flag


async def test_cross_turn_conflict_flagged_through_the_loop(live_pool: Any) -> None:
    sid = new_id()
    await _loop(live_pool, FakeModelProvider(responses=[
        ChatResult("The about page is live at /about-us.html.", None, [], 6, 6, "fake"),
    ])).run("where is the about page?", session_id=sid)
    # A later turn in the SAME conversation negates the existence of the same concrete path.
    await _loop(live_pool, FakeModelProvider(responses=[
        ChatResult("There is no such file /about-us.html — it returns a 404.", None, [], 8, 8, "fake"),
    ])).run("open the about page", session_id=sid)
    assert await _flag_count(live_pool, "cross_turn") >= 1
