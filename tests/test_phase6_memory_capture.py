"""PHASE 6 — reliability matrix · memory capture & the person graph.

The relationship extractor feeds the person graph (person:almir --rel--> person:<name>); the harden pass
found it minted GARBAGE nodes ('This'/'He'/'God'/'Google') as Almir's kin on the most natural phrasing.
The open-loop capture filed Sali's own chain-of-thought ("First, I need to understand…") as a durable
follow-up. This locks both fixes ([H]) plus the intro false-negatives that were also repaired.
"""

from __future__ import annotations

from sali.runtime.loop import (
    _OPEN_LOOP_FILLER_RE,
    _OPEN_LOOP_RE,
    _durable_signal,
    _extract_relationship,
)


# ---- relationship extraction never mints garbage nodes -------------------------------------------

def test_demonstrative_intro_resolves_to_the_real_name() -> None:  # [H] was ('This', married_to)
    assert _extract_relationship("This is my wife Faith") == ("Faith", "married_to")
    assert _extract_relationship("this is my wife Faith") == ("Faith", "married_to")


def test_capitalized_nonnames_mint_nothing() -> None:  # [H] no person:he / person:god / person:google
    for s in ("He is my brother", "She is my sister", "God is my father",
              "Nobody is my friend", "They are my team"):
        assert _extract_relationship(s) is None, s


def test_real_name_relationship_still_extracted() -> None:
    assert _extract_relationship("Faith is my wife") == ("Faith", "married_to")


# ---- the intro false-negatives the harden pass also fixed ----------------------------------------

def test_bare_name_after_rel_captured() -> None:
    assert _durable_signal("my brother Tom called") is True
    assert _extract_relationship("my brother Tom called") == ("Tom", "sibling_of")


def test_comma_appositive_captured() -> None:
    assert _extract_relationship("my wife, Faith, is here") == ("Faith", "married_to")


# ---- open-loop capture rejects internal planning narration ---------------------------------------

def _would_capture(reply: str) -> str | bool:
    """Mirror of loop._capture_open_loop's decision, so we test exactly what it would file."""
    m = _OPEN_LOOP_RE.search(reply)
    if not m:
        return False
    idx = m.start()
    left = reply.rfind(".", 0, idx)
    right = reply.find(".", idx)
    if left < 0:
        left = -1
    sentence = reply[left + 1: right if right >= 0 else len(reply)].strip()
    title = " ".join(sentence.split())[:120]
    if len(title) < 12 or _OPEN_LOOP_FILLER_RE.search(title):
        return False
    return title


def test_chain_of_thought_filler_not_captured() -> None:  # [H] the junk-row source
    for r in ("First, I need to understand what's going on.",
              "First, I need to understand the current situation.",
              "Okay, let me start by figuring out what is going on here."):
        assert _would_capture(r) is False, r


def test_genuine_open_loop_still_captured() -> None:
    for r in ("I'll look into the tanzahost billing discrepancy tomorrow.",
              "I need to investigate why the nginx config keeps reverting.",
              "Still need to follow up on the DNS migration for the client."):
        assert _would_capture(r), r
