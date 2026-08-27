"""Phase 1 · Increment 1 — Sali's runtime self-model (§6/§7/§41/§71/§84).

The persistent self-state survives across turns, composes the full self-view from existing stores
without duplicating them, and the self_state tool reports it. Self-knowledge is grounded, not invented.
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.config.settings import Settings
from sali.core.clock import SystemClock
from sali.core.enums import MemoryLayer, MemorySource
from sali.memory import writer
from sali.runtime.loop import _SelfSink
from sali.runtime.self_state import SELF_KNOWLEDGE, SelfStateStore
from sali.tools.builtins.self_tool import SelfState
from sali.tools.context import ToolContext

pytestmark = pytest.mark.db


async def test_self_state_singleton_is_seeded_and_updates(live_pool: Any) -> None:
    store = SelfStateStore(live_pool)
    initial = await store.get()
    assert initial["mode"] == "idle" and initial["turn_count"] == 0

    await store.note_turn("check the VPS disk usage")
    after = await store.get()
    assert after["mode"] == "working" and after["current_focus"] == "check the VPS disk usage"
    assert after["turn_count"] == 1

    await store.record_outcome(success=True, summary="reported disk at 42%")
    done = await store.get()
    assert done["mode"] == "idle" and done["last_success"] == "reported disk at 42%"
    assert done["last_success_at"] is not None


async def test_only_one_state_row_can_exist(live_pool: Any) -> None:
    import asyncpg

    async with live_pool.acquire() as conn:
        with pytest.raises(asyncpg.PostgresError):  # singleton: id can only be true
            await conn.execute("INSERT INTO sali_state (id) VALUES (false)")


async def test_assemble_composes_the_full_self_view(live_pool: Any) -> None:
    store = SelfStateStore(live_pool)
    await store.note_turn("set up the deploy")
    async with live_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO task (session_id, objective, status) VALUES (gen_random_uuid(), $1, 'running')",
            "deploy project X")
        # an uncertainty: a candidate memory that needs grounding
        await writer.remember(conn, layer=MemoryLayer.SEMANTIC, content="the cache is probably Redis",
                              source=MemorySource.INFERENCE)

    view = await store.assemble()
    assert view["identity"] == "Sali"
    assert view["self_knowledge"] == SELF_KNOWLEDGE and "Ollama" in view["self_knowledge"]
    assert view["current_focus"] == "set up the deploy"
    assert view["current_task"] == "deploy project X"          # composed from the task store
    assert view["uncertainty_count"] >= 1                       # composed from needs_grounding
    assert any("Redis" in u for u in view["uncertainties"])


async def test_self_state_tool_reports_through_the_sink(live_pool: Any) -> None:
    store = SelfStateStore(live_pool)
    await store.note_turn("investigate a failed build")
    ctx = ToolContext(settings=Settings(), clock=SystemClock(), self_model=_SelfSink(store))

    res = await SelfState().run({}, ctx)
    assert res.ok and res.output["identity"] == "Sali"
    assert "investigate a failed build" in res.display


async def test_tool_fails_cleanly_without_a_self_model() -> None:
    ctx = ToolContext(settings=Settings(), clock=SystemClock())  # no self_model injected
    res = await SelfState().run({}, ctx)
    assert not res.ok and "isn't available" in (res.error or "")
