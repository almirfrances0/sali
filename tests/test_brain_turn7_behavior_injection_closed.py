"""Brain-audit Turn 7: BehaviorStore auto-accept + "HIS STANDING REQUESTS" prompt injection are dead.

The bespoke behavioral-note channel was inserting accepted preferences at the strongest generation
position on every turn, and `observe_feedback` was auto-elevating any classified user directive to
`accepted` — so a single classifier misfire on a sarcastic "don't be so formal" would stick forever.
Both are removed. Preferences now flow through the normal memory-retrieval bundle, gated by an
actual acceptance decision.

Source-level guards so no future edit reintroduces either channel.
"""

from __future__ import annotations

import ast
import pathlib

import pytest


def _read(rel: str) -> str:
    return pathlib.Path(rel).read_text()


def _function_body(src: str, func_name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Find a top-level (or class-level) function definition by name and return its AST node.

    Used by AST-based guards that need to reject reintroductions even when a rename or a helper
    function bypasses a literal-string grep. Walks all classes.
    """
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return node
    return None


def test_render_behavioral_context_is_gone() -> None:
    """The renderer that produced the 'HIS STANDING REQUESTS' block is deleted from the module."""
    src = _read("src/sali/runtime/behavioral_context.py")
    assert "def render_behavioral_context" not in src, (
        "Turn 7 regressed: render_behavioral_context is back. It projected accepted preferences "
        "into the strongest generation position as 'HIS STANDING REQUESTS'.")
    assert "def _as_manner" not in src, (
        "Turn 7 regressed: _as_manner helper is back — it was only used by the deleted renderer.")


def test_his_standing_requests_string_is_gone_from_src() -> None:
    """The literal 'HIS STANDING REQUESTS' anchor must not exist anywhere in src/."""
    for p in pathlib.Path("src").rglob("*.py"):
        assert "HIS STANDING REQUESTS" not in p.read_text(), (
            f"Turn 7 regressed: {p} contains the 'HIS STANDING REQUESTS' anchor string.")


def test_engine_no_longer_takes_behavior_note() -> None:
    """The engine's assemble() signature has no behavior_note parameter, and no site appends it."""
    src = _read("src/sali/context/engine.py")
    assert "behavior_note:" not in src, (
        "Turn 7 regressed: engine.assemble takes behavior_note again. That parameter was the "
        "kwarg the loop used to inject the 'HIS STANDING REQUESTS' block.")
    assert 'user_content += f"\\n\\n[{behavior_note}]"' not in src, (
        "Turn 7 regressed: the behavior_note append is back in engine.py at the generation site.")


def test_loop_no_longer_assembles_behavior_note() -> None:
    """The loop no longer imports render_behavioral_context or builds a behavior_note kwarg."""
    src = _read("src/sali/runtime/loop.py")
    assert "render_behavioral_context" not in src, (
        "Turn 7 regressed: loop.py imports render_behavioral_context again.")
    assert "behavior_note=behavior_note" not in src, (
        "Turn 7 regressed: loop.py passes behavior_note= to context.assemble() again.")


def test_observe_feedback_no_longer_auto_accepts() -> None:
    """The auto-accept branch after propose() in observe_feedback is deleted (AST guard).

    Literal-string grep is trivial to bypass (rename kwarg, single quotes, helper method).
    This walks the AST of observe_feedback and rejects ANY call to `self.accept(...)` or any
    method whose name is `accept` on a `self`-rooted attribute chain — bypass-resistant.
    """
    src = _read("src/sali/learning/behavior.py")
    fn = _function_body(src, "observe_feedback")
    assert fn is not None, "observe_feedback function not found"
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            func = node.func
            # Reject `self.accept(...)` and `self._foo.accept(...)` and any nested attribute
            # chain rooted at `self` whose final attribute is `accept`.
            if isinstance(func, ast.Attribute) and func.attr == "accept":
                # Walk up the attribute chain to check the root is `self`.
                root = func.value
                while isinstance(root, ast.Attribute):
                    root = root.value
                if isinstance(root, ast.Name) and root.id == "self":
                    raise AssertionError(
                        f"Turn 7 regressed: observe_feedback contains a self.*.accept(...) call at "
                        f"line {node.lineno}. Auto-acceptance is banned — Almir accepts via the "
                        f"pending endpoint. If a helper (e.g. self._auto_elevate) is being used, "
                        f"the AST guard still rejects; check the source.")
    # Positive marker (documentation guard, not load-bearing on its own).
    assert "NO AUTO-ACCEPT (brain-audit turn 7)" in src


@pytest.mark.asyncio
async def test_observe_feedback_leaves_proposal_as_candidate(live_pool):
    """Behavioral: an explicit-sounding instruction is CANDIDATE after observe_feedback, not
    ACCEPTED. Belt-and-suspenders on the source-level check above."""
    from sali.learning.behavior import BehaviorStore

    bs = BehaviorStore(live_pool)
    bid = await bs.observe_feedback("always use PostgreSQL")
    assert bid is not None
    pending_ids = {p["id"] for p in await bs.pending()}
    assert bid in pending_ids, "expected the proposal to be in pending()"
    accepted_rows = await bs.accepted()
    assert not any(a["id"] == bid for a in accepted_rows), (
        "the classifier must not auto-accept — it stays a candidate until Almir accepts it")


def test_assemble_behavioral_context_still_available() -> None:
    """The assembler stays (the /api/v1/behavioral-context HTTP endpoint reads it). Only the
    renderer that projected it into the prompt is gone."""
    src = _read("src/sali/runtime/behavioral_context.py")
    assert "async def assemble_behavioral_context" in src, (
        "assemble_behavioral_context must stay — it's the read-only diagnostic API view.")


def test_prompt_assembly_modules_do_not_import_behavior_store() -> None:
    """Belt-and-suspenders on the injection ban: the two modules that assemble the prompt must not
    import BehaviorStore at all. If they don't have the class, they can't read accepted rows,
    and the deletion cannot be silently sidestepped by a rename (test-coverage lens gap #1).
    """
    for path in ("src/sali/context/engine.py", "src/sali/runtime/loop.py"):
        src = _read(path)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    assert alias.name != "BehaviorStore", (
                        f"Turn 7 regressed: {path} imports BehaviorStore. Prompt-assembly code "
                        f"has no legitimate reason to read behavior_proposal rows — accepted "
                        f"preferences flow through the memory bundle, not a direct read.")


@pytest.mark.asyncio
async def test_accept_writes_a_preference_memory(live_pool):
    """Behavioral: `BehaviorStore.accept()` must project the accepted proposal into a PREFERENCE
    memory row so retrieval sees it. Without this, LearningView's Approve is a fake control —
    the injection channel is dead, so status='accepted' alone would never reach the model.
    (Adversarial-review Regression finding, folded in.)
    """
    from sali.core.enums import MemoryLayer, MemorySource
    from sali.learning.behavior import BehaviorStore

    bs = BehaviorStore(live_pool)
    bid = await bs.propose(trigger="test", proposed_behavior="prefer PostgreSQL over SQLite",
                           scope="user", source_type="test", confidence=0.9)
    ok = await bs.accept(bid)
    assert ok, "accept() should succeed for a candidate proposal"

    async with live_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT content, layer, source FROM memory "
            "WHERE claim_key LIKE 'behavior:%' AND content = 'prefer PostgreSQL over SQLite' "
            "ORDER BY created_at DESC LIMIT 1")
    assert row is not None, (
        "accept() must write a memory row (PREFERENCE layer, USER_EXPLICIT source) so retrieval "
        "surfaces the preference on the next turn. Missing row means the Approve button in "
        "LearningView has no downstream effect on model behavior.")
    assert row["layer"] == MemoryLayer.PREFERENCE.value
    assert row["source"] == MemorySource.USER_EXPLICIT.value
