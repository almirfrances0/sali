"""Phase 1 · Increment 2 — live World-State "what's happening now?" (§5/§18/§72/§73).

Assembled from existing DB-shared signals (desktop.observed events, tool_execution) + a best-effort
perception probe; rendered compactly and injected into context so a turn already knows the environment.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from sali.context.engine import ContextEngine
from sali.provider.fake import FakeModelProvider
from sali.retrieval.models import RetrievalBundle
from sali.runtime.world_state import WorldState, WorldStateBuilder

pytestmark = pytest.mark.db


class _FakePerception:
    def __init__(self, app: str, title: str) -> None:
        self._app, self._title = app, title

    async def snapshot(self, *, ui: bool = False) -> dict[str, Any]:
        return {"available": True, "window": {"app": self._app, "title": self._title}, "ui": None}


async def _ran(conn: Any, command: str, *, success: bool, error: str | None = None) -> None:
    await conn.execute(
        "INSERT INTO tool_execution (run_id, tool_name, plan, success, error, status) "
        "VALUES ($1, 'execute_command', $2, $3, $4, 'observed')",
        uuid4(), {"args": {"command": command}}, success, error)


async def _observed(conn: Any, summary: str) -> None:
    await conn.execute(
        "INSERT INTO event (event_type, subject_type, payload) VALUES ('desktop.observed','desktop',$1)",
        {"summary": summary, "importance": 0.7})


async def test_world_state_assembles_from_shared_signals(live_pool: Any) -> None:
    async with live_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO task (session_id, objective, status) VALUES (gen_random_uuid(),$1,'running')",
            "deploy project X")
        await _observed(conn, "3 changes in project-x")
        await _ran(conn, "npm run dev", success=False, error="Error: port 3000 in use")
        await _ran(conn, "git status", success=True)

    builder = WorldStateBuilder(live_pool, _FakePerception("Code", "main.py — project-x"))
    ws = await builder.snapshot()

    assert ws.focused_app == "Code" and ws.focused_window == "main.py — project-x"
    assert ws.active_task == "deploy project X"
    assert "3 changes in project-x" in ws.recent_files
    assert {c.binary for c in ws.recent_commands} == {"npm", "git"}
    assert any(c.binary == "npm" and c.ok is False for c in ws.recent_commands)
    assert any("port 3000" in e for e in ws.recent_errors)


async def test_render_only_includes_lines_with_content(live_pool: Any) -> None:
    empty = WorldState()
    assert empty.is_empty() and empty.render() == ""

    ws = WorldState(focused_app="Firefox", active_task="research")
    text = ws.render()
    assert "Focused: Firefox" in text and "Working on: research" in text
    assert "Recent commands" not in text  # no empty lines


def test_world_note_becomes_a_context_section() -> None:
    engine = ContextEngine(FakeModelProvider(), ctx_tokens=4096)
    note = WorldState(focused_app="Code", recent_errors=["port 3000 in use"]).render()
    result = engine.assemble("why isn't this working?", RetrievalBundle(), [], world_note=note)
    assert "world" in result.included
    _ctx = "\n".join(m.content for m in result.messages)  # sections live in the user message now
    assert "Focused: Code" in _ctx and "port 3000" in _ctx


async def test_builder_without_perception_still_works(live_pool: Any) -> None:
    ws = await WorldStateBuilder(live_pool, None).snapshot()  # perception off/unavailable
    assert ws.focused_app is None  # degrades cleanly, no crash
