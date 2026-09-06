"""Two follow-ups from the last turn's audit:

    1. Corrections must supersede. The two-shape KEEP/SKIP parser dropped `about`, which is the
       supersession anchor used by _MemorySink.remember to build `claim_key = "remember:<topic>"`.
       Without it, a stated fact ("I work from Dar") could not replace an earlier one ("I work
       from Nairobi") - they piled up. `_infer_about` restores the anchor from the sentence.

    2. The _durable_signal gate was regex-based and too narrow: "my hosting business is Tanzahost",
       "you can email me at X", "i work from Dar" all missed. Now that the parser is reliable,
       widening the gate is safe - a false positive costs one SKIP; a false negative loses a memory.
"""

from __future__ import annotations

import pytest

from sali.runtime.loop import _infer_about


# ── _infer_about extracts the correction anchor from natural sentences ─────────────────────

@pytest.mark.parametrize("sentence,expected", [
    ("you use VS Code as your primary text editor on this machine", "editor"),
    ("your name is Almir", "name"),
    ("call me Sir", "name"),
    ("you work from Dar es Salaam", "location"),
    ("you live in Tanzania", "location"),
    ("your hosting business is Tanzahost", "business"),
    ("your role is developer", "role"),
    ("your favourite editor is Helix", "editor"),
    ("your company is Salieno", "company"),
    ("your email is almirfrances1@gmail.com", "email"),
])
def test_infer_about_extracts_the_topic(sentence: str, expected: str) -> None:
    """The correction anchor comes from the topic word in the sentence. Two statements with the
    same anchor supersede; two without one accumulate. Get the anchor right and 'i work from Dar
    now, not Nairobi' correctly retires the old memory."""
    assert _infer_about(sentence) == expected, sentence


@pytest.mark.parametrize("sentence", [
    "the sky is blue",
    "docker is installed",
    "it's cold today",
])
def test_infer_about_returns_none_for_unrecognised(sentence: str) -> None:
    """Conservative wins: prefer no anchor to a wrong one. A miss just means the sink applies its
    layer default (preferences still work; facts accumulate rather than supersede - noisy, not
    destructive)."""
    assert _infer_about(sentence) is None


# ── the gate now fires on the natural phrasings the brief's Prompt A named ─────────────────

@pytest.mark.parametrize("message,should_fire", [
    # Almir's actual examples from the brief:
    ("my hosting business is called Tanzahost.", True),
    ("I work from Dar es Salaam", True),
    ("You can email me at almir@example.com", True),
    ("my role is founder of Tanzahost", True),
    ("I dislike bullet-list-only replies", True),
    ("I want dark mode by default", True),
    # False positives from earlier iterations (must stay unmatched):
    ("thanks", False),
    ("hey sali", False),
    ("what is the disk usage right now?", False),
])
def test_widened_gate_matches_the_natural_shapes(message: str, should_fire: bool) -> None:
    """The gate now matches structural fact-statements Almir speaks conversationally, not just the
    explicit "remember this" preamble. Widening was safe because the KEEP/SKIP parser (last turn's
    fix) reliably declines cases the model considers non-durable, so a false positive on the gate
    just costs one extra silent SKIP."""
    from sali.runtime.loop import _durable_signal

    assert _durable_signal(message, reply="") == should_fire, message


# ── end-to-end: a correction that used to accumulate now supersedes ────────────────────────

@pytest.mark.db
async def test_a_correction_supersedes_the_prior_fact(db_conn) -> None:  # noqa: ANN001
    """The regression I introduced when the two-shape parser dropped `about`: a stated fact could
    not replace an earlier statement on the same topic. Restored by _infer_about, which extracts a
    stable anchor from the sentence, which _MemorySink then hashes into a claim_key, which the
    writer uses for supersession."""
    from sali.core.enums import MemoryLayer, MemorySource
    from sali.memory import writer

    # First statement lands as a functional claim under a topic-anchored key.
    first = await writer.remember(
        db_conn, layer=MemoryLayer.SEMANTIC,
        content="You work from Dar es Salaam.",
        source=MemorySource.USER_EXPLICIT,
        functional=True, claim_key="remember:location",
    )
    # A later contradictory statement under the same anchor must retire the first.
    second = await writer.remember(
        db_conn, layer=MemoryLayer.SEMANTIC,
        content="You work from Nairobi.",
        source=MemorySource.USER_EXPLICIT,
        functional=True, claim_key="remember:location",
    )
    assert second.id != first.id
    current = await db_conn.fetch(
        "SELECT content FROM memory WHERE claim_key='remember:location' "
        "AND valid_until IS NULL AND superseded_by IS NULL")
    assert [r["content"] for r in current] == ["You work from Nairobi."]
    prior = await db_conn.fetchrow(
        "SELECT valid_until, superseded_by FROM memory WHERE id=$1", first.id)
    assert prior["valid_until"] is not None and prior["superseded_by"] == second.id
