"""Intent, evidence and self-knowledge: the behaviours that separate acting from sounding right.

Directive §28 scenarios covered here: (3)/(4) an action is claimed but nothing ran, (7) Almir cancels
running work, (17)/(18) memory of a cancelled objective must not read as current intent, (20)/(24) Sali
refers to his own machine as his own, and (14) resource pressure is something he can notice.

As in test_truthfulness.py, nothing asserts on phrasing that a model produces. Every assertion is on a
deterministic function's output or on database state.
"""

from __future__ import annotations

import pytest

from sali.runtime.attention import AttentionCategory, classify
from sali.runtime.loop import _render_tool_record
from sali.tasks.revocation import classify_revocation


# ── "stop" must stop, and the two layers must cover it between them ─────────────────────────────────

def _is_stopped(message: str) -> bool:
    """Either layer stopping the work counts: attention preempts the turn in flight, revocation makes
    the abandonment durable so no recovery or scheduler path revives it."""
    decision = classify(message, has_primary=True, current_objective="build the thing")
    return (decision.category is AttentionCategory.CANCEL_PRIMARY_TASK
            or classify_revocation(message))


@pytest.mark.parametrize("message", [
    "Stop that.",       # every one of these is an example the directive gives for §17
    "Forget it.",
    "Leave it.",
    "Don't continue.",
    "stop",
    "stop it",
    "never mind",
    "cancel that task",
    "forget about that project",
    "drop that idea",
    "abandon it",
    "scrap it",
])
def test_calling_work_off_is_understood(message: str) -> None:
    assert _is_stopped(message), f"{message!r} left the work running"


@pytest.mark.parametrize("message", [
    "don't forget it",          # the OPPOSITE instruction — this one abandoned the work
    "dont forget that",
    "never forget that",
    "do not cancel that task",
    "stop the server",          # an instruction about a server, not about Sali's work
    "can you leave it running in the background",
    "what did you stop doing yesterday?",
])
def test_the_opposite_instruction_does_not_stop_the_work(message: str) -> None:
    """A revocation cannot be taken back from inside the conversation, so a false positive here is the
    expensive direction: "don't forget it" matched the unanchored "forget it" and abandoned the very
    work Almir was asking Sali to hold on to."""
    assert not _is_stopped(message), f"{message!r} wrongly cancelled the work"


# ── the stall judge must weigh the claim against the record ─────────────────────────────────────────

def test_a_turn_with_no_tools_says_so_plainly() -> None:
    """The judge used to see only the prose, so a confident "Done — I created it" read as finished.
    Its evidence line has to state the absence, not omit it."""
    assert _render_tool_record([]) == "nothing — you called no tools"


def test_a_failed_tool_is_not_evidence_that_the_work_happened() -> None:
    rendered = _render_tool_record([("create_file", False)])
    assert "FAILED" in rendered and "worked" not in rendered


def test_the_record_keeps_successes_and_failures_apart() -> None:
    rendered = _render_tool_record([("web_search", True), ("create_file", False)])
    assert rendered == "web_search (worked), create_file (FAILED)"


# ── Sali's own machine is his own ───────────────────────────────────────────────────────────────────

def test_sali_does_not_describe_his_own_home_in_the_third_person() -> None:
    """The possessive was planted by the system, not picked up by the model: the identity prefix, the
    machine-awareness note and a durable twin memory all called it "Almir's machine", on up to 100% of
    turns. Ownership is still stated — it IS Almir's — but the name Sali is given for where he lives is
    "this machine"."""
    from sali.context.engine import IDENTITY
    from sali.runtime.self_state import SELF_KNOWLEDGE

    for text in (IDENTITY, SELF_KNOWLEDGE):
        assert "Almir's machine" not in text
        assert "Almir's PC" not in text
    assert "Almir" in IDENTITY, "ownership must still be stated, not quietly dropped"


def test_the_fallback_self_description_cannot_drift_from_the_grounded_one() -> None:
    """They were duplicate prose that had to stay byte-identical; editing one was all it took to make
    Sali's two self-descriptions disagree."""
    from sali.runtime.self_state import SELF_KNOWLEDGE, _compose_self_knowledge

    assert SELF_KNOWLEDGE == _compose_self_knowledge({})


# ── noticing the machine, not just being stopped by it ──────────────────────────────────────────────

async def test_gpu_pressure_is_readable_outside_the_lease() -> None:
    """The lease already REFUSES an over-committing generation. This is the other half — Sali being
    able to SAY the card is under pressure. On a host with no GPU it must return None rather than
    inventing a reading."""
    from sali.provider.ollama import gpu_pressure

    snap = await gpu_pressure()
    if snap is None:
        pytest.skip("no GPU on this host")
    temp, vram = snap
    assert 0.0 <= vram <= 1.0
    assert 0 < temp < 130


def test_the_pressure_thresholds_are_the_ones_tuned_for_this_machine() -> None:
    """A resident 35B model on a 12GB card sits at ~90% VRAM in its healthy steady state. Generic
    thresholds read that as permanently critical and shed every piece of background work forever."""
    from sali.runtime.resources import ResourceBudget

    budget = ResourceBudget()
    assert budget.vram_high > 0.90, "healthy residency must not read as pressure"
    assert budget.vram_high < budget.vram_critical < budget.vram_emergency
