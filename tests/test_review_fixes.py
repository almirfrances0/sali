"""Tests for the review fixes: failure paths (spec §34), multi-turn continuity, embed
resilience, redaction, and code/SQL freshness consistency."""

from __future__ import annotations

from typing import Any

import pytest

from sali.config.settings import DbSettings, ModelSettings, Settings
from sali.context.engine import ContextEngine
from sali.core.enums import FreshnessPolicy, MemoryLayer, MemorySource, RiskLevel
from sali.core.ids import new_id
from sali.memory import writer as mem_writer
from sali.memory.decay import FRESHNESS_MAX_AGE
from sali.memory.embed_worker import embed_pending_conn
from sali.provider.base import ChatMessage, ChatResult, ToolCall, ToolSpec
from sali.provider.fake import FakeModelProvider
from sali.retrieval.service import RetrievalService
from sali.runtime.loop import AgentLoop
from sali.security.confirm import AutoAllowConfirmer
from sali.security.policy import PolicyEngine
from sali.tools.base import Tool, ToolResult
from sali.tools.registry import ToolRegistry, default_registry

pytestmark = pytest.mark.db


def _settings() -> Settings:
    return Settings(db=DbSettings(name="sali_test"), model=ModelSettings(provider="fake"))


def _loop(
    pool: Any,
    provider: FakeModelProvider,
    *,
    registry: ToolRegistry | None = None,
    policy: PolicyEngine | None = None,
) -> AgentLoop:
    return AgentLoop(
        pool=pool,
        provider=provider,
        retrieval=RetrievalService(pool, provider),
        context=ContextEngine(provider, ctx_tokens=8192),
        registry=registry or default_registry(),
        policy=policy or PolicyEngine(),
        confirmer=AutoAllowConfirmer(),
        settings=_settings(),
    )


class _FailingTool(Tool):
    name = "always_fails"
    description = "test-only"
    risk_level = RiskLevel.R0

    async def run(self, args: dict[str, Any], ctx: Any) -> ToolResult:
        return ToolResult(ok=False, output={}, display="nope", error="boom")


class _RaisingProvider(FakeModelProvider):
    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec] | None = None,
        options: dict[str, Any] | None = None,
        think: bool = False,
    ) -> ChatResult:
        raise RuntimeError("model down")


async def test_tool_failure_is_recorded_as_failure_not_success(live_pool: Any) -> None:
    reg = ToolRegistry()
    reg.register(_FailingTool())
    fake = FakeModelProvider(
        responses=[
            ChatResult("", None, [ToolCall("always_fails", {})], 5, 2, "fake"),
            ChatResult("I could not complete that.", None, [], 5, 5, "fake"),
        ]
    )
    result = await _loop(live_pool, fake, registry=reg).run("do the thing")
    async with live_pool.acquire() as c:
        te = await c.fetchrow(
            "SELECT status, success FROM tool_execution WHERE run_id=$1", result.run_id
        )
        assert te["status"] == "verified_failure" and te["success"] is False
        failed = await c.fetchval(
            "SELECT count(*) FROM event WHERE event_type='tool.failed' AND subject_id=$1",
            result.run_id,
        )
        assert failed == 1


async def test_provider_failure_marks_run_failed(live_pool: Any) -> None:
    with pytest.raises(RuntimeError):
        await _loop(live_pool, _RaisingProvider()).run("hello")
    async with live_pool.acquire() as c:
        row = await c.fetchrow("SELECT status FROM agent_runs ORDER BY started_at DESC LIMIT 1")
        assert row["status"] == "failed"


async def test_denied_tool_is_audited_and_never_executes(live_pool: Any) -> None:
    reg = ToolRegistry()
    reg.register(_FailingTool())
    policy = PolicyEngine(denylist=frozenset({"always_fails"}))
    fake = FakeModelProvider(
        responses=[
            ChatResult("", None, [ToolCall("always_fails", {})], 5, 2, "fake"),
            ChatResult("Understood, I won't.", None, [], 5, 5, "fake"),
        ]
    )
    result = await _loop(live_pool, fake, registry=reg, policy=policy).run("delete everything")
    async with live_pool.acquire() as c:
        audit = await c.fetchrow(
            "SELECT decision, ok FROM tool_audit WHERE run_id=$1", result.run_id
        )
        assert audit["decision"] == "deny" and audit["ok"] is None
        denied = await c.fetchval(
            "SELECT count(*) FROM event WHERE event_type='tool.denied' AND subject_id=$1",
            result.run_id,
        )
        assert denied == 1
        # a denied tool never reaches EXECUTE, so there is no tool_execution row
        assert await c.fetchval(
            "SELECT count(*) FROM tool_execution WHERE run_id=$1", result.run_id
        ) == 0


async def test_multi_turn_conversation_is_persisted_and_carried(live_pool: Any) -> None:
    fake = FakeModelProvider(
        responses=[
            ChatResult("Hi Almir.", None, [], 5, 5, "fake"),
            ChatResult("Yes — you greeted me.", None, [], 5, 5, "fake"),
        ]
    )
    loop = _loop(live_pool, fake)
    session = new_id()
    await loop.run("hello", session_id=session)
    await loop.run("did I greet you?", session_id=session)

    async with live_pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM conversation WHERE id=$1", session) == 1
        assert await c.fetchval(
            "SELECT count(*) FROM message WHERE conversation_id=$1", session
        ) == 4  # 2 user + 2 assistant
        assert await c.fetchval(
            "SELECT count(*) FROM event WHERE event_type='conversation.created' AND subject_id=$1",
            session,
        ) == 1

    # The second turn's context actually saw the first turn (continuity, not a fresh session).
    last_system = next(m for m in fake.calls[-1]["messages"] if m.role == "system")
    assert "Conversation so far" in last_system.content
    assert "hello" in last_system.content


async def test_embed_failure_leaves_rows_pending_not_error(db_conn: Any) -> None:
    class _BadEmbed(FakeModelProvider):
        async def embed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("embed down")

    await mem_writer.remember(
        db_conn, layer=MemoryLayer.SEMANTIC, content="a fact to embed",
        source=MemorySource.USER_EXPLICIT,
    )
    n = await embed_pending_conn(db_conn, _BadEmbed(), 32)
    assert n == 0
    status = await db_conn.fetchval(
        "SELECT embed_status FROM memory WHERE content='a fact to embed'"
    )
    assert status == "pending"  # retryable — a transient blip must not permanently drop recall


async def test_freshness_rule_table_matches_code(db_conn: Any) -> None:
    rows = await db_conn.fetch("SELECT policy, extract(epoch FROM max_age) AS secs FROM freshness_rule")
    table = {r["policy"]: float(r["secs"]) for r in rows}
    for policy, delta in FRESHNESS_MAX_AGE.items():
        assert policy.value in table
        if policy is not FreshnessPolicy.PERMANENT:  # both are 'effectively never'; skip exact
            assert abs(table[policy.value] - delta.total_seconds()) < 1.0
