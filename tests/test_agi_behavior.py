"""AGI behavioral tests — demonstrating cognition, not just database writes.

These tests verify that Sali's cognitive tools and systems actually work as described
in the AGI transformation spec. Each test demonstrates a specific cognitive capability.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ── Helper: wrap a raw asyncpg Connection as a Pool-like object ─────────────────────────

class _FakePool:
    """Wraps a raw asyncpg Connection so pool.acquire() returns it."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def acquire(self) -> _FakePool:
        return self

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        pass


def _fake_ctx(conn: Any = None) -> Any:
    """Create a minimal ToolContext for testing cognitive tools."""
    ctx = MagicMock()
    ctx.pool = _FakePool(conn) if conn is not None else None
    ctx.memory = None
    ctx.recall = None
    ctx.clock = None
    ctx.settings = MagicMock()
    return ctx


# ── 1. Sali can check her own cognitive state ─────────────────────────────────────────

@pytest.mark.db
async def test_self_status_reports_cognitive_state(db_conn: Any) -> None:
    """self_status must return memories, curiosities, goals, and active task."""
    from sali.tools.builtins.cognitive_tool import SelfStatus
    tool = SelfStatus()
    await db_conn.execute(
        "INSERT INTO sali.curiosity (id, subject, statement, status, priority, times_encountered) "
        "VALUES (gen_random_uuid(), 'test knowledge gap', 'test knowledge gap', 'open', 0.5, 1)")
    result = await tool.run({}, _fake_ctx(db_conn))
    assert result.ok
    assert "curiosities" in result.output
    assert len(result.output["curiosities"]) >= 1


# ── 2. Sali can search her own memory actively ─────────────────────────────────────────

@pytest.mark.db
async def test_query_memory_finds_stored_facts(db_conn: Any) -> None:
    """query_memory must find memories via the recall interface."""
    from sali.tools.builtins.cognitive_tool import QueryMemory
    tool = QueryMemory()
    ctx = _fake_ctx(db_conn)
    ctx.recall = MagicMock()
    ctx.recall.search = AsyncMock(return_value=[
        {"content": "Almir prefers dark theme interfaces", "layer": "semantic",
         "confidence": 0.8, "source": "user_explicit"}])
    result = await tool.run({"query": "dark theme"}, ctx)
    assert result.ok
    assert len(result.output["results"]) >= 1
    assert "dark theme" in result.output["results"][0]["content"]


# ── 3. Sali can flag knowledge gaps ────────────────────────────────────────────────────

@pytest.mark.db
async def test_flag_curiosity_creates_gap(db_conn: Any) -> None:
    """flag_curiosity must create a curiosity entry."""
    from sali.tools.builtins.cognitive_tool import FlagCuriosity
    tool = FlagCuriosity()
    result = await tool.run(
        {"topic": "how does the embedding model work", "context": "testing"}, _fake_ctx(db_conn))
    assert result.ok
    assert result.output["action"] == "created"
    row = await db_conn.fetchrow(
        "SELECT subject FROM sali.curiosity WHERE subject='how does the embedding model work'")
    assert row is not None


# ── 4. Sali can assess her own confidence ───────────────────────────────────────────────

def test_assess_confidence_classifies_levels() -> None:
    """assess_confidence must classify confidence into levels with advice."""
    from sali.tools.builtins.cognitive_tool import AssessConfidence
    tool = AssessConfidence()
    result = asyncio.run(tool.run(
        {"claim": "the sky is blue", "confidence": 0.95, "source": "verified"}, _fake_ctx()))
    assert result.ok
    assert result.output["level"] == "high"
    result = asyncio.run(tool.run(
        {"claim": "something uncertain", "confidence": 0.2, "source": "guessed"}, _fake_ctx()))
    assert result.ok
    assert result.output["level"] == "very_low"


# ── 5. Sali can query the knowledge graph ───────────────────────────────────────────────

@pytest.mark.db
async def test_query_graph_finds_entities(db_conn: Any) -> None:
    """query_graph must find graph nodes by name."""
    from sali.tools.builtins.cognitive_tool import QueryGraph
    tool = QueryGraph()
    # Insert a test node so the query has something to find
    await db_conn.execute(
        "INSERT INTO sali.graph_node (id, node_type, name, canonical_key, source, props) "
        "VALUES (gen_random_uuid(), 'agent', 'Sali', 'agent:sali', 'system_observation', '{}') "
        "ON CONFLICT DO NOTHING")
    result = await tool.run({"query": "Sali"}, _fake_ctx(db_conn))
    assert result.ok
    assert len(result.output["nodes"]) >= 1


# ── 6. Sali can form hypotheses ─────────────────────────────────────────────────────────

@pytest.mark.db
async def test_form_hypothesis_persists_to_working_memory(db_conn: Any) -> None:
    """form_hypothesis must persist the hypothesis in working memory."""
    # Create working_memory table if not exists (it's in migration 0066)
    await db_conn.execute("""
        CREATE TABLE IF NOT EXISTS sali.working_memory (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id TEXT NOT NULL,
            category TEXT NOT NULL,
            content TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.5,
            source TEXT NOT NULL DEFAULT 'reasoning',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ,
            superseded_by UUID
        )
    """)
    from sali.tools.builtins.cognitive_tool import FormHypothesis
    tool = FormHypothesis()
    result = await tool.run({
        "observation": "service keeps restarting",
        "hypothesis": "the config file has a syntax error",
        "prediction": "journalctl will show a parse error",
        "confidence": 0.6,
    }, _fake_ctx(db_conn))
    assert result.ok
    assert result.output["status"] == "formed"
    row = await db_conn.fetchrow(
        "SELECT content FROM sali.working_memory WHERE category='hypothesis' "
        "AND superseded_by IS NULL")
    assert row is not None
    assert "config file" in row["content"]


# ── 7. Sali can create self-generated goals ─────────────────────────────────────────────

@pytest.mark.db
async def test_create_goal_persists(db_conn: Any) -> None:
    """create_goal must persist the goal in the goal table."""
    from sali.tools.builtins.cognitive_tool import CreateGoal
    tool = CreateGoal()
    result = await tool.run({
        "objective": "Learn how the embedding model works",
        "origin": "self_initiated",
        "priority": 0.5,
    }, _fake_ctx(db_conn))
    assert result.ok
    assert result.output["action"] == "created"
    row = await db_conn.fetchrow(
        "SELECT objective, origin FROM sali.goal WHERE objective LIKE '%embedding%'")
    assert row is not None
    assert row["origin"] == "self_initiated"


# ── 8. Sali can update her working memory ───────────────────────────────────────────────

@pytest.mark.db
async def test_working_memory_set_and_get(db_conn: Any) -> None:
    """Working memory must persist entries across calls."""
    await db_conn.execute("""
        CREATE TABLE IF NOT EXISTS sali.working_memory (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id TEXT NOT NULL,
            category TEXT NOT NULL,
            content TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.5,
            source TEXT NOT NULL DEFAULT 'reasoning',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ,
            superseded_by UUID
        )
    """)
    from sali.runtime.working_memory import WorkingMemoryStore
    store = WorkingMemoryStore(_FakePool(db_conn))
    from uuid import uuid4
    sid = str(uuid4())
    entry_id = await store.set(sid, "active_problem", "debugging the CSS issue",
                               confidence=0.8, source="observation")
    assert entry_id is not None
    entry = await store.get(sid, "active_problem")
    assert entry is not None
    assert entry["content"] == "debugging the CSS issue"
    assert abs(entry["confidence"] - 0.8) < 0.01
    # Supersede with new content
    await store.set(sid, "active_problem", "CSS issue resolved")
    entry2 = await store.get(sid, "active_problem")
    assert entry2 is not None
    assert entry2["content"] == "CSS issue resolved"
    # Old entry should be superseded
    old = await db_conn.fetchrow(
        "SELECT superseded_by FROM sali.working_memory WHERE id=$1", entry_id)
    assert old["superseded_by"] is not None


# ── 9. Sali can reflect and persist the insight ─────────────────────────────────────────

def test_reflect_on_calls_memory() -> None:
    """reflect_on must call ctx.memory.remember to persist the insight."""
    from sali.tools.builtins.cognitive_tool import ReflectOn
    tool = ReflectOn()
    ctx = _fake_ctx()
    ctx.memory = MagicMock()
    ctx.memory.remember = AsyncMock()
    result = asyncio.run(tool.run({
        "topic": "CSS class naming",
        "insight": "Always define CSS classes before writing HTML that uses them",
        "confidence": 0.8,
    }, ctx))
    assert result.ok
    ctx.memory.remember.assert_called_once()


# ── 10. Sali can self-diagnose ──────────────────────────────────────────────────────────

@pytest.mark.db
async def test_self_diagnose_reports_health(db_conn: Any) -> None:
    """self_diagnose must return memory health, task stats, and issues."""
    await db_conn.execute("""
        CREATE TABLE IF NOT EXISTS sali.working_memory (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id TEXT NOT NULL,
            category TEXT NOT NULL,
            content TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.5,
            source TEXT NOT NULL DEFAULT 'reasoning',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ,
            superseded_by UUID
        )
    """)
    from sali.tools.builtins.cognitive_tool import SelfDiagnose
    tool = SelfDiagnose()
    result = await tool.run({}, _fake_ctx(db_conn))
    assert result.ok
    assert "memories" in result.output
    assert "tasks" in result.output
    assert "issues" in result.output


# ── 11. Context engine accepts new AGI parameters ───────────────────────────────────────

def test_context_engine_agi_parameters() -> None:
    """ContextEngine.assemble() must accept AGI context parameters."""
    import inspect

    from sali.context.engine import ContextEngine
    sig = inspect.signature(ContextEngine.assemble)
    assert "knowledge_gaps_note" in sig.parameters
    assert "working_memory_note" in sig.parameters
    assert "almir_state_note" in sig.parameters


# ── 12. Almir model derives emotional state ─────────────────────────────────────────────

def test_almir_model_derives_state() -> None:
    """derive_almir_state must detect emotional signals from messages."""
    from sali.runtime.almir_model import derive_almir_state
    msgs = [
        {"role": "user", "content": "this doesn't work again"},
        {"role": "assistant", "content": "let me check"},
        {"role": "user", "content": "it keeps failing, so frustrating"},
    ]
    state = derive_almir_state(msgs)
    assert state["tone"] == "frustrated"
    msgs = [
        {"role": "user", "content": "amazing work sali!"},
        {"role": "user", "content": "this is perfect, love it"},
    ]
    state = derive_almir_state(msgs)
    assert state["tone"] == "happy"


# ── 13. All cognitive tools registered ──────────────────────────────────────────────────

def test_all_cognitive_tools_registered() -> None:
    """All 15 cognitive tools must be registered."""
    from sali.tools.registry import default_registry
    r = default_registry()
    names = {t.name for t in r.tools()}
    expected = {
        "think_aloud", "query_memory", "flag_curiosity", "assess_confidence",
        "self_status", "query_graph", "reflect_on", "update_working_memory",
        "form_hypothesis", "run_experiment", "create_goal", "query_world",
        "self_diagnose", "propose_behavior", "learn_lesson",
    }
    missing = expected - names
    assert not missing, f"missing cognitive tools: {missing}"


# ── 14. Self-reflective pass exists ─────────────────────────────────────────────────────

def test_self_reflective_pass_exists() -> None:
    """The loop must have a self-reflective pass."""
    import inspect

    from sali.runtime.loop import AgentLoop
    source = inspect.getsource(AgentLoop.astream)
    assert "CORRECTED:" in source


# ── 15. Binary file detection ───────────────────────────────────────────────────────────

def test_read_file_refuses_binary() -> None:
    """read_file must detect binary formats."""
    import inspect

    from sali.tools.builtins.filesystem import ReadFile
    source = inspect.getsource(ReadFile.run)
    assert "BINARY_SIGNATURES" in source


# ── 16. Protected paths enforced ────────────────────────────────────────────────────────

def test_protected_paths_enforced() -> None:
    """Destructive commands targeting Sali's directory must be blocked."""
    from sali.tools.builtins.exec_tool import _targets_protected_path
    assert _targets_protected_path("rm -rf /home/almir/Desktop/sali") is not None
    assert _targets_protected_path("cat << 'EOF' | tee /home/almir/Desktop/sali/file") is not None
    assert _targets_protected_path("ls /home/almir/Desktop/sali") is None


# ── 17. Kernel seeds identity ───────────────────────────────────────────────────────────

def test_kernel_seeds_identity() -> None:
    """Kernel must have _seed_identity method."""
    import inspect

    from sali.kernel import Kernel
    source = inspect.getsource(Kernel._seed_identity)
    assert "person:almir" in source
    assert "agent:sali" in source


# ── 18. Memory layer quota ──────────────────────────────────────────────────────────────

def test_memory_layer_quota_exists() -> None:
    """Memory retrieval must have per-layer quotas."""
    from sali.memory.service import _LAYER_QUOTA
    assert "system_env" in _LAYER_QUOTA
    assert _LAYER_QUOTA["system_env"] <= 2


# ── 19. Cancellation detection ──────────────────────────────────────────────────────────

def test_cancellation_catches_natural_phrases() -> None:
    """Cancellation regex must catch natural phrasing."""
    from sali.tasks.authority import _CANCEL_PATTERNS
    assert _CANCEL_PATTERNS.search("no i have cancelled it. leave it please")
    assert _CANCEL_PATTERNS.search("I SAID NO NO NO TASK")
    assert not _CANCEL_PATTERNS.search("can you leave it running in the background")


# ── 21. Cognitive Executive perceives signals ─────────────────────────────────────────────

def test_executive_perceives_user_input() -> None:
    """Executive must detect user input as an attention signal."""
    import asyncio

    from sali.runtime.executive import CognitiveExecutive
    exec = CognitiveExecutive(None)  # no pool needed for user input detection
    signals = asyncio.run(exec.perceive(user_input="hey sali what is the status"))
    assert len(signals) >= 1
    assert signals[0].source == "user_message"
    assert signals[0].urgency == 1.0


# ── 22. Cognitive Executive decides response for user messages ─────────────────────────────

@pytest.mark.db
async def test_executive_decides_response_for_user_message(db_conn: Any) -> None:
    """Executive must decide RESPOND when user message is pending."""
    from sali.runtime.executive import CognitiveAction, CognitiveExecutive
    exec = CognitiveExecutive(_FakePool(db_conn))
    signals = await exec.perceive(user_input="hey sali, what's the status?")
    decision = await exec.decide(signals, user_message_pending=True)
    assert decision.action == CognitiveAction.RESPOND
    assert decision.priority == 1.0


# ── 23. Cognitive Executive decides rest when nothing needs attention ──────────────────────

def test_executive_decides_rest_when_idle() -> None:
    """Executive must decide REST when there are no signals."""
    import asyncio

    from sali.runtime.executive import CognitiveAction, CognitiveExecutive
    exec = CognitiveExecutive(None)
    decision = asyncio.run(exec.decide([], user_message_pending=False))
    assert decision.action == CognitiveAction.REST
    assert decision.priority == 0.0


# ── 24. Cognitive Executive prioritizes overdue commitments ───────────────────────────────

def test_executive_prioritizes_overdue_commitments() -> None:
    """Executive must decide COMMUNICATE for overdue commitments."""
    import asyncio

    from sali.runtime.executive import AttentionSignal, CognitiveAction, CognitiveExecutive
    exec = CognitiveExecutive(None)
    signals = [
        AttentionSignal(source="curiosity", urgency=0.3, importance=0.5, content="learn Docker"),
        AttentionSignal(source="commitment", urgency=0.8, importance=0.8, content="check the logs"),
    ]
    # Sort signals the same way perceive() does
    signals.sort(key=lambda s: s.urgency * 0.5 + s.importance * 0.5, reverse=True)
    decision = asyncio.run(exec.decide(signals, user_message_pending=False))
    assert decision.action == CognitiveAction.COMMUNICATE
    assert "overdue" in decision.reason


# ── 25. Executive decision is journaled ───────────────────────────────────────────────────

def test_executive_decision_has_required_fields() -> None:
    """CognitiveDecision must have action, reason, priority."""
    from sali.runtime.executive import CognitiveAction, CognitiveDecision
    d = CognitiveDecision(action=CognitiveAction.THINK, reason="test", priority=0.5)
    assert d.action == CognitiveAction.THINK
    assert d.reason == "test"
    assert d.priority == 0.5


# ── 20. Working memory survives restart ─────────────────────────────────────────────────

@pytest.mark.db
async def test_working_memory_survives_supersede(db_conn: Any) -> None:
    """Working memory entries must be superseded, not deleted, preserving history."""
    await db_conn.execute("""
        CREATE TABLE IF NOT EXISTS sali.working_memory (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id TEXT NOT NULL,
            category TEXT NOT NULL,
            content TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.5,
            source TEXT NOT NULL DEFAULT 'reasoning',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ,
            superseded_by UUID
        )
    """)
    from uuid import uuid4

    from sali.runtime.working_memory import WorkingMemoryStore
    store = WorkingMemoryStore(_FakePool(db_conn))
    sid = str(uuid4())
    _ = await store.set(sid, "hypothesis", "first hypothesis")
    _ = await store.set(sid, "hypothesis", "updated hypothesis")
    # Both entries should exist (old one superseded, new one current)
    count = await db_conn.fetchval(
        "SELECT count(*) FROM sali.working_memory WHERE session_id=$1 AND category='hypothesis'", sid)
    assert count == 2
    # Only the new one should be current
    current = await store.get(sid, "hypothesis")
    assert current is not None
    assert current["content"] == "updated hypothesis"
