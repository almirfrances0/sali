"""Brain-audit Turn 4: the voice/honesty duplicates in the always-on tail block are gone.

Source-level guard: any future edit that re-adds "no offer of help / no what's next /
no sign-off" or the "Never claim you looked something up" prohibition to engine.py
regresses this test. The task-rule sentence (plan_task guidance) stays because its
own comment marks it as load-bearing.
"""

from __future__ import annotations

import pathlib


def _engine_src() -> str:
    return pathlib.Path("src/sali/context/engine.py").read_text()


def test_voice_anchor_prohibitions_gone() -> None:
    """The five assistant-speak bans stacked at the generation point are deleted.
    IDENTITY at the top of the prompt already carries warmth (warm/direct/present)."""
    src = _engine_src()
    assert "no offer of help" not in src, (
        "the 'no offer of help / no what's next / no sign-off' voice-anchor is back "
        "in engine.py; Turn 4 regressed - Sali will collapse to short curt replies again")
    assert "\"what's next\"" not in src


def test_honesty_prohibition_gone_from_tail() -> None:
    """The 'Never claim you looked something up' clause is deleted from the tail
    (SECURITY_NOTE at the top of the prompt already covers 'never claim you did
    something you didn't actually do')."""
    src = _engine_src()
    assert "Never claim you looked something up" not in src, (
        "duplicate honesty prohibition is back in the tail; Turn 4 regressed")


def test_task_rule_still_present() -> None:
    """Load-bearing sentence kept. Without it Sali stopped calling plan_task and
    fabricated pages (documented in the deleted block's own comment)."""
    src = _engine_src()
    assert "plan_task" in src
    assert "Real multi-step work goes through plan_task" in src


def test_no_prior_9_constraint_stack() -> None:
    """The pre-audit tail stacked 9 constraints, then got rebalanced to 4, still bad.
    Now: exactly one clause, phrased positively. Guard the ACTUAL STRING that gets appended
    to user_content (not the comments that document its history)."""
    src = _engine_src()
    # The kept sentence is inside "[Real multi-step work goes through plan_task ...]".
    marker = "[Just talk — no assistant openers"
    i = src.find(marker)
    assert i > 0, "Turn 4 kept sentence missing"
    j = src.find("]", i)
    sentence = src[i:j+1].lower()
    prohibitions = sum(1 for word in ["never", "do not", "don't"]
                       if word in sentence)
    assert prohibitions == 0, (
        f"Turn 4 kept sentence has {prohibitions} prohibitions - meant to be positive-phrased: {sentence!r}")
