"""Sali Web · Increment A1 — real deltas on the durable bus.

The web brain must animate TRUTH, not a timer. These verify that the small, low-volume signals a live
UI needs are actually emitted: presence changes (idle↔working), task lifecycle, a run-start marker, and
a retrieval-activation frame naming the REAL memories/edges a turn used. All ride the existing
`event` table + sali_events NOTIFY, or the astream LoopEvent stream the terminal and /ws already share.
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.core.ids import new_id
from sali.provider.base import ChatResult
from sali.provider.fake import FakeModelProvider
from sali.runtime.self_state import SelfStateStore
from sali.tasks.store import TaskStore
from tests.test_agent_loop import _loop  # reuse the wired AgentLoop builder

pytestmark = pytest.mark.db


async def _events(conn: Any, event_type: str) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT payload FROM event WHERE event_type=$1 ORDER BY seq", event_type)
    return [dict(r["payload"]) for r in rows]


# ── presence (§27) ────────────────────────────────────────────────────────────────────────────────
async def test_presence_events_on_working_and_idle(live_pool: Any) -> None:
    store = SelfStateStore(live_pool)
    await store.note_turn("look at the disk")
    await store.record_outcome(success=True, summary="looked at the disk")
    async with live_pool.acquire() as conn:
        modes = [p["mode"] for p in await _events(conn, "self.presence")]
    assert modes[-2:] == ["working", "idle"]  # a turn boundary pushes both transitions


async def test_presence_payload_carries_no_raw_focus_text(live_pool: Any) -> None:
    # the raw user text (focus) must NOT be persisted in the presence event — only the mode
    store = SelfStateStore(live_pool)
    await store.note_turn("my secret token is hunter2")
    async with live_pool.acquire() as conn:
        payloads = await _events(conn, "self.presence")
    assert payloads and all(set(p) == {"mode"} for p in payloads)


# ── tasks (were invisible to the bus) ───────────────────────────────────────────────────────────────
async def test_task_lifecycle_events(live_pool: Any) -> None:
    store = TaskStore(live_pool)
    task = await store.create("ship the web UI", ["scaffold", "wire ws"])
    await store.advance(task.id, 1, "done")
    await store.finish(task.id, status="done")
    async with live_pool.acquire() as conn:
        created = await _events(conn, "task.created")
        advanced = await _events(conn, "task.step_advanced")
        finished = await _events(conn, "task.finished")
        subj = await conn.fetchval(
            "SELECT subject_id FROM event WHERE event_type='task.created' ORDER BY seq DESC LIMIT 1")
    assert any(c["steps"] == 2 for c in created)
    assert any(a["seq"] == 1 and a["status"] == "done" for a in advanced)
    assert any(f["status"] == "done" for f in finished)
    assert subj == task.id  # subject_id ties the event to the task for the UI


# ── the astream run + retrieval frames (terminal ignores unknown kinds; /ws forwards them) ───────────
async def test_astream_emits_run_marker_first(live_pool: Any) -> None:
    fake = FakeModelProvider(responses=[ChatResult("hi", None, [], 2, 2, "fake")])
    kinds = []
    run_data = None
    async for ev in _loop(live_pool, fake).astream("hello", session_id=new_id()):
        kinds.append(ev.kind)
        if ev.kind == "run":
            run_data = ev.data
    assert kinds[0] == "run"  # the run id is announced before anything else
    assert run_data and "run_id" in run_data and "session_id" in run_data


async def test_astream_retrieval_frame_names_real_nodes(live_pool: Any) -> None:
    # seed a memory so retrieval has something real to surface, then assert the retrieval frame
    # carries that memory's id (a real node to light up) — never invented activity.
    from sali.core.enums import MemoryLayer, MemorySource
    from sali.memory.service import MemoryService

    mem = MemoryService(live_pool, FakeModelProvider())
    m = await mem.remember(layer=MemoryLayer.SEMANTIC, content="the GPU is an RTX 4070",
                           source=MemorySource.SYSTEM_OBSERVATION)
    fake = FakeModelProvider(responses=[ChatResult("it's a 4070", None, [], 2, 2, "fake")])
    frames = [ev async for ev in _loop(live_pool, fake).astream("what GPU?", session_id=new_id())
              if ev.kind == "retrieval"]
    assert frames, "a turn that retrieves must emit a retrieval-activation frame"
    all_ids = {mid for f in frames for mid in f.data.get("memories", [])}
    assert str(m.id) in all_ids  # the REAL retrieved memory, by id
