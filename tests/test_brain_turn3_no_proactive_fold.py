"""Brain-audit Turn 3: proactive intra-turn compaction fold is gone; only true
provider-overflow triggers _compact_and_continue.

This is a source-level guard - we assert the proactive branch is either deleted or
gated dead, so a future edit doesn't quietly re-enable it. The overflow-recovery
branch stays live; its reason string is `provider_overflow`. Regression protection.
"""

from __future__ import annotations

import pathlib


def test_proactive_compaction_branch_is_dead() -> None:
    """The `if len(messages) > 3 and pressure.should_compact:` branch that was firing on
    every provider call at 50% budget is now dead (rolled behind `if False and ...`).
    A live edit removing the `False and` guard would regress this test."""
    src = pathlib.Path("src/sali/runtime/loop.py").read_text()
    assert 'if False and len(messages) > 3 and pressure.should_compact' in src, (
        "the proactive compaction fold has been re-enabled; Almir's words will be "
        "replaced by an LLM paraphrase every time budget crosses 50% - Turn 3 regressed")


def test_provider_overflow_recovery_still_present() -> None:
    """The overflow recovery branch (reason='provider_overflow') MUST remain - it's the
    only safety net now that proactive folding is gone."""
    src = pathlib.Path("src/sali/runtime/loop.py").read_text()
    assert 'reason="provider_overflow"' in src, (
        "provider_overflow recovery was deleted along with the proactive fold - "
        "Sali can no longer recover from an actual context overflow")


def test_context_approaching_event_still_emits() -> None:
    """Even without proactive folding we still emit context_approaching_limit so
    operators can watch pressure without the mid-turn side-effect."""
    src = pathlib.Path("src/sali/runtime/loop.py").read_text()
    assert 'runtime.context_approaching_limit' in src
