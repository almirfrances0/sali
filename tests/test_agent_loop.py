"""The full journaled agent loop, end to end (with a scripted fake model)."""

from __future__ import annotations

from typing import Any

import pytest

from sali.config.settings import DbSettings, ModelSettings, Settings
from sali.context.engine import ContextEngine
from sali.core.ids import new_id
from sali.provider.base import ChatResult, ToolCall
from sali.provider.fake import FakeModelProvider
from sali.retrieval.service import RetrievalService
from sali.runtime.loop import AgentLoop
from sali.security.confirm import AutoAllowConfirmer
from sali.security.policy import PolicyEngine
from sali.tools.registry import default_registry

pytestmark = pytest.mark.db


def _settings() -> Settings:
    return Settings(db=DbSettings(name="sali_test"), model=ModelSettings(provider="fake"))


def _loop(pool: Any, provider: FakeModelProvider) -> AgentLoop:
    return AgentLoop(
        pool=pool,
        provider=provider,
        retrieval=RetrievalService(pool, provider),
        context=ContextEngine(provider, ctx_tokens=8192),
        registry=default_registry(),
        policy=PolicyEngine(),
        confirmer=AutoAllowConfirmer(),
        settings=_settings(),
    )


async def test_loop_calls_tool_then_answers_and_journals(live_pool: Any) -> None:
    fake = FakeModelProvider(
        responses=[
            ChatResult("", None, [ToolCall("memory_info", {})], 10, 5, "fake"),
            ChatResult("You have plenty of memory available.", None, [], 20, 8, "fake"),
        ]
    )
    result = await _loop(live_pool, fake).run("how much memory is free right now?")

    assert result.text == "You have plenty of memory available."
    assert result.tool_calls == 1

    async with live_pool.acquire() as c:
        run = await c.fetchrow(
            "SELECT status, state FROM agent_runs WHERE run_id=$1", result.run_id
        )
        assert run["status"] == "completed"
        assert run["state"] == "done"

        te = await c.fetchrow(
            "SELECT tool_name, status, success FROM tool_execution WHERE run_id=$1", result.run_id
        )
        assert te["tool_name"] == "memory_info"
        assert te["status"] == "verified_success"  # never assumed — actually verified
        assert te["success"] is True

        # journaled: at least retrieve + 2 llm_calls + tool + respond
        events = await c.fetchval(
            "SELECT count(*) FROM run_events WHERE run_id=$1", result.run_id
        )
        assert events >= 5
        llm_calls = await c.fetchval(
            "SELECT count(*) FROM run_events WHERE run_id=$1 AND kind='llm_call'", result.run_id
        )
        assert llm_calls == 2
        audits = await c.fetchval(
            "SELECT count(*) FROM tool_audit WHERE run_id=$1", result.run_id
        )
        assert audits == 1


async def test_loop_denies_unknown_tool_but_continues(live_pool: Any) -> None:
    fake = FakeModelProvider(
        responses=[
            ChatResult("", None, [ToolCall("rm_rf_slash", {})], 5, 2, "fake"),
            ChatResult("I could not use that tool, but I'm still here.", None, [], 5, 5, "fake"),
        ]
    )
    result = await _loop(live_pool, fake).run("do something dangerous")
    assert "still here" in result.text
    async with live_pool.acquire() as c:
        run = await c.fetchrow("SELECT status FROM agent_runs WHERE run_id=$1", result.run_id)
        assert run["status"] == "completed"  # an unknown tool never crashes the loop
        # no tool_execution row for a tool that never existed
        assert await c.fetchval(
            "SELECT count(*) FROM tool_execution WHERE run_id=$1", result.run_id
        ) == 0


async def test_loop_recover_surfaces_interrupted_run(live_pool: Any) -> None:
    # Simulate a crash: a run left 'running' in EXECUTE_TOOL with a non-idempotent tool.
    async with live_pool.acquire() as c:
        run_id = await c.fetchval(
            "INSERT INTO agent_runs (run_id, session_id, user_input, state, status) "
            "VALUES (gen_random_uuid(), gen_random_uuid(), 'x', 'execute_tool', 'running') "
            "RETURNING run_id"
        )
        await c.execute(
            "INSERT INTO tool_execution (run_id, tool_name, status, danger_level, approved_by, plan) "
            "VALUES ($1, 'git_pull', 'executing', 2, 'policy:confirm', '{}')",
            run_id,
        )

    fake = FakeModelProvider()
    resolved = await _loop(live_pool, fake).recover()

    assert len(resolved) == 1
    assert resolved[0]["was_state"] == "execute_tool"
    # git_pull is a known, non-idempotent tool → verify against reality, never blind re-run (M15).
    assert resolved[0]["action"] == "verify_then_continue"
    async with live_pool.acquire() as c:
        status = await c.fetchval("SELECT status FROM agent_runs WHERE run_id=$1", run_id)
        assert status == "aborted"


async def test_astream_streams_tokens_then_final(live_pool: Any) -> None:
    fake = FakeModelProvider(responses=[ChatResult("hello there friend", None, [], 3, 3, "fake")])
    kinds: list[str] = []
    final_text = ""
    async for event in _loop(live_pool, fake).astream("hi", session_id=new_id()):
        kinds.append(event.kind)
        if event.kind == "final":
            final_text = event.text
    assert "token" in kinds and "final" in kinds  # the answer streamed, then finalized
    assert final_text == "hello there friend"


async def test_astream_emits_tool_events(live_pool: Any) -> None:
    fake = FakeModelProvider(
        responses=[
            ChatResult("", None, [ToolCall("memory_info", {})], 3, 2, "fake"),
            ChatResult("plenty free", None, [], 3, 3, "fake"),
        ]
    )
    starts = [
        e async for e in _loop(live_pool, fake).astream("ram?", session_id=new_id())
        if e.kind == "tool" and e.data.get("phase") == "start"
    ]
    assert len(starts) == 1 and starts[0].data["name"] == "memory_info"


async def test_conversation_compacts_when_long(db_conn: Any) -> None:
    from sali.runtime.session import persistent_session_id  # noqa: F401 (import-shape check)

    session = new_id()
    await db_conn.execute("INSERT INTO conversation (id) VALUES ($1)", session)
    for i in range(30):
        await db_conn.execute(
            "INSERT INTO message (conversation_id, seq, role, content) VALUES ($1,$2,$3,$4)",
            session, i + 1, "user" if i % 2 == 0 else "assistant", f"turn number {i}",
        )
    loop = _loop(None, FakeModelProvider())  # pool unused by _maybe_compact / _load_history
    await loop._maybe_compact(db_conn, session)

    conv = await db_conn.fetchrow(
        "SELECT summary, summary_through_seq FROM conversation WHERE id=$1", session
    )
    assert conv["summary"] is not None
    assert conv["summary_through_seq"] == 30 - 6  # kept the last 6 verbatim
    history = await loop._load_history(db_conn, session)
    assert history[0][0] == "earlier"  # the running summary leads the history
    assert len(history) <= 9  # summary + at most 8 recent
