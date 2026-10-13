"""Brain-audit Turn 5: assistant-speak bans stripped from IDENTITY + LIVE_NOTE reduced to empty.

Source-level guards. Test the IMPORTED string values (not the file text - Turn 4's comment
mentions historical prohibitions verbatim, and typographic apostrophes differ from straight)."""

from __future__ import annotations

from sali.context.engine import IDENTITY, LIVE_NOTE, SECURITY_NOTE


def test_identity_no_longer_enumerates_assistant_speak_bans() -> None:
    """The 5 script-shaped bans are gone from the IDENTITY string that ships to the model."""
    for banned in ["How can I help you", "As an AI", "corporate tone",
                   "announcing that you", "no disclaimers"]:
        assert banned not in IDENTITY, (
            f"Turn 5 regressed: {banned!r} is back in IDENTITY - should have been stripped")


def test_identity_still_states_positive_voice() -> None:
    """The kept positive statement is the whole voice principle now."""
    assert "digital person" in IDENTITY, "positive identity statement lost"
    assert "You just talk" in IDENTITY, "natural-voice principle lost"


def test_live_note_reduced_to_empty() -> None:
    """LIVE_NOTE is empty (duplicated IDENTITY). The `if live_note:` guard in assemble()
    treats empty as falsy, so no section is appended when the loop passes LIVE_NOTE."""
    assert LIVE_NOTE == "", (
        f"LIVE_NOTE should be empty (duplicated IDENTITY), got: {LIVE_NOTE!r}")


def test_security_note_still_carries_honesty() -> None:
    """After Turn 4 dropped the tail's honesty clause, SECURITY_NOTE is the sole carrier.
    Check by keyword, not exact-string (typographic apostrophe differs from straight)."""
    lower = SECURITY_NOTE.lower()
    assert "never claim" in lower and "actually do" in lower, (
        f"Turn 5 over-trimmed: SECURITY_NOTE lost its honesty clause. Got: {SECURITY_NOTE!r}")
