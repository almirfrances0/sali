"""What must survive when a conversation is folded away.

Compaction is only safe if the state-changing facts outlive the turns that carried them. The two most
consequential things Almir can say — "that's wrong, it's actually Y" and "stop, drop it" — had no slot
in the packet schema at all, so once their turns were folded they existed nowhere: a correction kept
only inside DONE or FACTS is indistinguishable from the claim it replaced, and a cancellation with no
home can be silently inherited by the next summary as live work.
"""

from __future__ import annotations

from sali.runtime.continuation import PACKET_INSTRUCTION, SECTIONS, build_packet, parse_sections


def test_the_schema_has_somewhere_to_put_a_correction_and_a_cancellation() -> None:
    assert "CORRECTED" in SECTIONS
    assert "STOPPED" in SECTIONS


def test_they_are_read_back_before_the_lines_they_override() -> None:
    """A cancellation that appears after the DONE/NEXT it contradicts is easy to read past."""
    assert SECTIONS.index("STOPPED") < SECTIONS.index("DONE")
    assert SECTIONS.index("CORRECTED") < SECTIONS.index("DONE")


def test_an_unknown_label_never_loses_its_content() -> None:
    """The parser is deliberately tolerant: a line it does not recognise is folded into the section
    above rather than dropped. That is the right trade for a summary — a mislabelled correction that
    survives in the wrong field is recoverable, one that is silently deleted is not.

    The real hazard this guards is different: a label added to the PROMPT but not to SECTIONS is
    recognised by neither branch, so the whole schema change becomes a no-op that nothing reports."""
    parsed = parse_sections("STOPPED: Almir cancelled the deploy\nMISLABELLED: and do not restart it")
    assert "cancelled the deploy" in parsed["stopped"]
    assert "do not restart it" in parsed["stopped"], "content must survive, even in the wrong field"

    for label in ("STOPPED", "CORRECTED"):
        assert label in PACKET_INSTRUCTION, f"{label} is in SECTIONS but never asked for"
        assert parse_sections(f"{label}: something"), f"{label} is asked for but not parsed"


def test_a_real_folded_turn_keeps_the_cancellation_and_the_correction() -> None:
    summary = (
        "STOPPED: Almir said to stop the nginx migration and not resume it.\n"
        "CORRECTED: I had recorded the editor as Neovim; Almir corrected it to VS Code, which is what "
        "is actually on PATH.\n"
        "DONE: read the backup script; confirmed the archive exists.\n"
        "NEXT: inspect one archive's contents.\n"
        "FACTS: /home/almir/Desktop/tar-test/output contains four .tar.gz files (checked on disk).\n"
        "OPEN: whether the archives hold the expected relative paths.\n"
    )
    notes = build_packet(None, summary)["notes"]
    assert "stop the nginx migration" in notes["stopped"]
    assert "Neovim" in notes["corrected"] and "VS Code" in notes["corrected"], (
        "both halves must survive — the old claim alone reads as current, the new alone loses the fix")
    assert "archive exists" in notes["done"]
    assert notes["open"], "uncertainty must not be dropped"


def test_the_instruction_forbids_promoting_a_guess_into_a_fact() -> None:
    """Compaction is where an "I think" quietly becomes an assertion, because a summary has no room for
    hedging unless it is asked for."""
    lowered = PACKET_INSTRUCTION.lower()
    assert "uncertainty uncertain" in lowered or "stays a guess" in lowered
    assert "never promote" in lowered


def test_the_instruction_stops_its_own_formatting_rules_leaking_into_the_output() -> None:
    """Observed live: CONSTRAINTS came back as "first person, compact, exact labels only" — the
    summariser recorded its own formatting rules as if Almir had imposed them, and that then rode into
    every later turn as a standing constraint to respect."""
    assert "never anything about how to write this summary" in PACKET_INSTRUCTION
    assert "do not describe these formatting rules" in PACKET_INSTRUCTION
