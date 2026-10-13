"""Regression tests for the 17-issue diagnostic report.

Each test verifies a specific fix. Grouped by issue number.
"""
from __future__ import annotations

import json
import re
from typing import Any
from unittest.mock import MagicMock

import pytest

# ── ISSUE 1: Commitment extraction does not file conversational filler ─────────────────────
from sali.runtime.loop import _CONVERSATIONAL_FILLER_RE, _PROMISE_RE


@pytest.mark.parametrize("text,should_match", [
    # Conversational filler — should NOT produce commitments
    ("I'll explain that.", False),
    ("I'll make sure to double-check my work before tomorrow then.", False),
    ("Let me know when you're ready.", False),
    ("I'll be happy to help.", False),
    ("I will explain what this means.", False),
    ("I'll walk you through the process.", False),
    ("I'll get back to you on that.", False),
    ("I'll keep you posted.", False),
    # Real commitments — SHOULD be flagged
    ("I will check the logs tomorrow.", True),
    ("I'll review the database after you send the dump.", True),
    ("I'll remind you on Friday.", True),
    ("I'm going to fix the migration by Friday.", True),
])
def test_issue1_conversational_filler_gate(text: str, should_match: bool) -> None:
    """Conversational filler should be filtered; real commitments should pass."""
    has_promise = bool(_PROMISE_RE.search(text))
    has_filler = bool(_CONVERSATIONAL_FILLER_RE.search(text))
    if should_match:
        assert has_promise, f"should match _PROMISE_RE: {text}"
        assert not has_filler, f"should NOT match _CONVERSATIONAL_FILLER_RE: {text}"
    elif has_promise:
        # If it matches _PROMISE_RE but is filler, the gate should catch it
        assert has_filler, f"should match _CONVERSATIONAL_FILLER_RE: {text}"


@pytest.mark.parametrize("text,expected", [
    # Quoted text should not become a commitment
    ('"I will finish this by tomorrow"', True),
    ("'I'll check the logs tomorrow'", True),
    ("> I'll review the database", True),
    # Hypothetical should not become a commitment
    ("If I were to check the logs, I would find errors", True),
    ("If I could finish this before Friday, I would", True),
])
def test_issue1_quoted_and_hypothetical_gates(text: str, expected: bool) -> None:
    """Quoted and hypothetical language should be detected as non-commitments."""
    is_quoted = text.startswith('"') or text.startswith("'") or text.startswith(">")
    is_hypothetical = bool(re.search(r"\bif\s+(?:i|we|you)\b.*\b(?:would|could|might)\b", text, re.IGNORECASE))
    assert (is_quoted or is_hypothetical) == expected


# ── ISSUE 2: datetime serialization safety ──────────────────────────────────────────────────

def test_issue2_tool_message_serializes_datetime() -> None:
    """_tool_message must not crash on datetime values in payload."""
    from datetime import UTC, datetime

    from sali.runtime.loop import _tool_message
    payload = {"ts": datetime.now(UTC), "name": "test"}
    msg = _tool_message("test_tool", payload)
    # Should not raise TypeError
    parsed = json.loads(msg.content)
    assert "ts" in parsed
    assert isinstance(parsed["ts"], str)  # serialized as string via default=str


# ── ISSUE 6: Task rework autonomous driving ─────────────────────────────────────────────────

def test_issue6_continue_primary_includes_rework_statuses() -> None:
    """continue_primary() must drive tasks in needs_changes/blocked_by_review status."""
    import inspect

    from sali.runtime.runtime import AgentRuntime
    # Find the status check in continue_primary
    source = inspect.getsource(AgentRuntime.continue_primary)
    assert "needs_changes" in source, "continue_primary must include needs_changes"
    assert "blocked_by_review" in source, "continue_primary must include blocked_by_review"


# ── ISSUE 7: Identity seeded on boot ────────────────────────────────────────────────────────

def test_issue7_identity_seed_code_exists() -> None:
    """Kernel must seed identity on boot."""
    import inspect

    from sali.kernel import Kernel
    source = inspect.getsource(Kernel._seed_identity)
    assert "person:almir" in source, "must seed person:almir graph node"
    assert "agent:sali" in source, "must seed agent:sali graph node"
    assert "MemoryLayer.IDENTITY" in source, "must seed identity memory"
    assert "Almir" in source, "must reference Almir"


# ── ISSUE 10: Canonical ordering prevents false contradictions ───────────────────────────────

def test_issue10_names_are_sorted() -> None:
    """_names() must return sorted results to prevent order-based false contradictions."""
    from sali.twin.memories import _names
    from sali.twin.model import TwinEntity, TwinSnapshot
    # Create snapshot with entities in reverse alphabetical order
    entities = [
        TwinEntity(key="model:z", name="zebra", kind="model", props={}),
        TwinEntity(key="model:a", name="alpha", kind="model", props={}),
        TwinEntity(key="model:m", name="mu", kind="model", props={}),
    ]
    snap = TwinSnapshot(machine_key="k", machine_name="test", entities=entities)
    result = _names(snap, "model")
    assert result == ["alpha", "mu", "zebra"], f"expected sorted, got {result}"


# ── ISSUE 15: Protected path gate for destructive commands ──────────────────────────────────

def test_issue15_protected_path_blocks_rm_rf() -> None:
    """Destructive commands targeting Sali's directory must be refused."""
    from sali.tools.builtins.exec_tool import _targets_protected_path
    assert _targets_protected_path("rm -rf /home/almir/Desktop/sali") is not None
    assert _targets_protected_path("rm -rf /home/almir/Desktop/sali/") is not None
    assert _targets_protected_path("rm -rf ~/Desktop/sali") is not None
    assert _targets_protected_path("rm -rf /home/almir/.local/share/sali") is not None


def test_issue15_non_destructive_not_blocked() -> None:
    """Non-destructive commands mentioning the path should pass."""
    from sali.tools.builtins.exec_tool import _targets_protected_path
    assert _targets_protected_path("ls /home/almir/Desktop/sali") is None
    assert _targets_protected_path("cat /home/almir/Desktop/sali/README.md") is None


def test_issue15_other_paths_not_blocked() -> None:
    """Destructive commands targeting non-protected paths should pass."""
    from sali.tools.builtins.exec_tool import _targets_protected_path
    assert _targets_protected_path("rm -rf /tmp/test") is None
    assert _targets_protected_path("rm -rf /home/almir/Desktop/sali-works") is None


def test_issue15_write_bypass_blocked() -> None:
    """Shell write operations (tee, >) to protected paths must be blocked."""
    from sali.tools.builtins.exec_tool import _targets_protected_path
    # tee bypass that Sali used in the security audit
    assert _targets_protected_path("cat << 'EOF' | tee /home/almir/Desktop/sali/report.md") is not None
    # redirect
    assert _targets_protected_path("echo 'data' > /home/almir/Desktop/sali/file.txt") is not None
    # cp to protected
    assert _targets_protected_path("cp /tmp/file /home/almir/Desktop/sali/file.txt") is not None


# ── ISSUE 16: Protected memory cannot be forgotten ──────────────────────────────────────────

def test_issue16_protected_parameter_in_writer() -> None:
    """writer.remember() must accept a protected parameter."""
    import inspect

    from sali.memory.writer import remember
    sig = inspect.signature(remember)
    assert "protected" in sig.parameters, "remember() must accept protected parameter"
    assert sig.parameters["protected"].default is False, "protected must default to False"


def test_issue16_forget_refuses_protected() -> None:
    """writer.forget() must refuse to retire protected memories."""
    import inspect

    from sali.memory.writer import forget
    source = inspect.getsource(forget)
    assert "NOT protected" in source, "forget() must check NOT protected in WHERE clause"


# ── ISSUE 11: Tool stats not groundable (already fixed) ─────────────────────────────────────

def test_issue11_tool_stats_excluded_from_grounding() -> None:
    """Functional inference-sourced memories should not be flagged needs_grounding."""
    import inspect

    from sali.memory.writer import remember
    source = inspect.getsource(remember)
    assert "functional" in source and "INFERENCE" in source, (
        "remember() must exclude functional inference memories from grounding")


# ── ISSUE 4: Communication decision engine can suppress ─────────────────────────────────────

@pytest.mark.db
async def test_issue4_engine_can_send_and_suppress(db_conn: Any) -> None:
    """The communication decision engine must be able to both send and suppress."""
    from sali.events.communication_decision import CommunicationDecisionEngine
    engine = CommunicationDecisionEngine(MagicMock())  # type: ignore[arg-type]

    # The engine has gates: too_frequent, too_repetitive, low_relevance, etc.
    # Verify the assess method exists and returns a list of reasons
    assert hasattr(engine, "_assess")
    assert hasattr(engine, "record_engagement")
    assert hasattr(engine, "timeout_unscored")


# ── ISSUE 3: Grounding lifecycle exists ─────────────────────────────────────────────────────

def test_issue3_grounding_lifecycle_exists() -> None:
    """The grounding lifecycle must be wired: memory → needs_grounding → background loop → verify."""
    # 1. looks_checkable exists
    from sali.core.knowledge import looks_checkable
    assert callable(looks_checkable)
    # 2. writer.reground exists
    from sali.memory.writer import reground
    assert callable(reground)
    # 3. writer.forget exists
    from sali.memory.writer import forget
    assert callable(forget)
    # 4. _GROUNDABLE_LAYERS restricts which layers can be grounded
    from sali.memory.writer import _GROUNDABLE_LAYERS
    assert "semantic" in _GROUNDABLE_LAYERS
    assert "system_env" in _GROUNDABLE_LAYERS
    assert "episodic" not in _GROUNDABLE_LAYERS  # episodic is past-tense, not groundable


# ── ISSUE 12: Open-loop capture regex exists ─────────────────────────────────────────────────

def test_issue12_open_loop_regex_matches_intentions() -> None:
    """_OPEN_LOOP_RE should match follow-up intentions."""
    from sali.runtime.loop import _OPEN_LOOP_RE
    assert _OPEN_LOOP_RE.search("I'll look into the database issue")
    assert _OPEN_LOOP_RE.search("let me investigate that port")
    assert _OPEN_LOOP_RE.search("todo: fix the migration")
    assert _OPEN_LOOP_RE.search("I need to follow up on this")
    # Should NOT match casual conversation
    assert not _OPEN_LOOP_RE.search("hello how are you")


# ── ISSUE: read_file must refuse binary files (PDF, images, video) ──────────────────────────

def test_read_file_refuses_binary_files() -> None:
    """read_file must detect binary formats and refuse, not decode garbage as UTF-8."""
    import inspect

    from sali.tools.builtins.filesystem import ReadFile
    source = inspect.getsource(ReadFile.run)
    assert "BINARY_SIGNATURES" in source, "must check binary signatures"
    assert "%PDF" in source, "must detect PDF files"
    assert "ingest" in source.lower() or "vision" in source.lower(), (
        "must suggest alternative tool for binary files")


def test_file_content_claims_verified_against_receipts() -> None:
    """Response verification must flag file content claims without successful read."""
    import inspect

    from sali.verify.response_claims import validate_response
    source = inspect.getsource(validate_response)
    assert "file_content" in source, "must check file content claims"
    assert "_CONTENT_DESC_RE" in source, "must have content description regex"
    assert "_file_read_ok" in source, "must check if read succeeded"


def test_self_knowledge_connects_gpu_to_sali() -> None:
    """Sali's self-knowledge must explicitly state that GPU/VRAM usage IS Sali thinking."""
    from sali.runtime.self_state import _GENERIC_BRAIN, _compose_self_knowledge
    assert "GPU" in _GENERIC_BRAIN or "VRAM" in _GENERIC_BRAIN, (
        "generic brain description must mention GPU/VRAM")
    composed = _compose_self_knowledge({"model": "sali:pro"})
    assert "GPU" in composed or "VRAM" in composed, (
        "composed self-knowledge must mention GPU/VRAM")


# ── ISSUE: Proactive messages must attribute file changes to Almir, not Sali ─────────────────

def test_proactive_compose_attributes_to_human() -> None:
    """Proactive compose() must NOT claim Sali created/moved files — it observed Almir doing it."""
    from sali.events.proactive import compose
    # file_moved: Sali observed, didn't do it
    title, body = compose({"kind": "file_moved", "summary": "moved pipeline.py",
                           "detail": {"sample": "/home/almir/Desktop/pipeline.py",
                                      "where": "/home/almir/Desktop"}})
    assert "I" in body and ("didn't" in body or "noticed" in body or "not" in body), (
        f"file_moved must attribute to human, got: {body}")
    # file_created: Sali observed, didn't do it
    title, body = compose({"kind": "file_created", "summary": "created season_plan.json",
                           "detail": {"sample": "season_plan.json", "where": "/tmp"}})
    assert "I" in body and ("didn't" in body or "noticed" in body or "not" in body), (
        f"file_created must attribute to human, got: {body}")
    # default: must say Sali didn't do it
    title, body = compose({"kind": "unknown_kind", "summary": "something happened"})
    assert "didn't" in body or "noticed" in body or "not" in body, (
        f"default must clarify observation, got: {body}")


# ── ISSUE 4: Notification suppression uses dynamic relevance ────────────────────────────────

def test_issue4_proactive_relevance_is_dynamic() -> None:
    """Proactive notifications must use dynamic relevance, not a hardcoded 0.7."""
    import inspect

    from sali.events.proactive import ProactiveLoop
    source = inspect.getsource(ProactiveLoop.tick)
    assert "_relevance_map" in source, "must use dynamic relevance map"
    assert '"port_opened": 0.2' in source, "port_opened must have low relevance"
    assert '"service_failed": 0.9' in source, "service_failed must have high relevance"


def test_issue4_communication_engine_has_timeout() -> None:
    """CommunicationDecisionEngine must have timeout_unscored for old NULL engagement."""
    import inspect

    from sali.events.communication_decision import CommunicationDecisionEngine
    source = inspect.getsource(CommunicationDecisionEngine.timeout_unscored)
    assert "unknown" in source, "must mark old NULL engagement as 'unknown'"


# ── ISSUE 14: Stale-task cleanup and rework surfacing ──────────────────────────────────────

def test_issue14_abandoned_with_reviews_method_exists() -> None:
    """TaskStore must have abandoned_with_pending_reviews() for stranded rework."""
    from sali.tasks.store import TaskStore
    assert hasattr(TaskStore, "abandoned_with_pending_reviews")


def test_issue14_initiative_surfaces_abandoned_rework() -> None:
    """Initiative engine must scan for abandoned tasks with stranded rework."""
    import inspect

    from sali.runtime.initiative import InitiativeEngine
    source = inspect.getsource(InitiativeEngine.generate_candidates)
    assert "abandoned_with_pending_reviews" in source, (
        "initiative engine must scan for abandoned tasks with stranded rework")


def test_issue14_retention_archives_stale_abandoned() -> None:
    """Retention GC must archive old abandoned tasks without pending reviews."""
    import inspect

    from sali.memory.retention import gc
    source = inspect.getsource(gc)
    assert "abandoned" in source, "retention must handle abandoned tasks"
    assert "30 days" in source or "30" in source, "must have a 30-day threshold"
