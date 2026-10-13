"""Every label the summarizer is asked for must survive the round trip back into the prompt.

`SECTIONS` and `render_packet` were two hand-maintained lists that had to agree and did not:
STOPPED, CORRECTED and THREAD were parsed, stored, and then silently dropped on render. Each of the
three was a live failure on 2026-09-07 — a corrected claim repeated in the next sentence, a
cancelled task resurrected, and catastrophic personal news answered from hardware because the one
field carrying "how Almir is" never reached the model.
"""
from __future__ import annotations

from sali.runtime import continuation
from sali.runtime.continuation import SECTIONS, render_packet


def _packet() -> dict:
    return {"notes": {s.lower(): f"value-for-{s.lower()}" for s in SECTIONS}}


def test_every_stored_label_is_rendered() -> None:
    out = render_packet(_packet())
    missing = [s for s in SECTIONS if f"{s}:" not in out]
    assert not missing, f"parsed and stored, then dropped on the way back in: {missing}"


def test_what_was_taken_back_leads() -> None:
    """STOPPED and CORRECTED must land BEFORE the DONE/NEXT they override — continuation.py's own
    comment says so, and before this was fixed neither was emitted at all."""
    out = render_packet(_packet())
    assert out.index("STOPPED:") < out.index("DONE:")
    assert out.index("CORRECTED:") < out.index("DONE:")


def test_the_human_thread_survives() -> None:
    """The slot whose stated purpose is that "a personal moment isn't flattened into a task ledger"."""
    out = render_packet({"notes": {
        "done": "Explained the Nexus failure.",
        "thread": "Almir told me his wife left him. He is not okay.",
    }})
    assert "THREAD: Almir told me his wife left him. He is not okay." in out


def test_an_empty_label_costs_nothing() -> None:
    out = render_packet({"notes": {"done": "one thing"}})
    assert out.strip() == "DONE: one thing"


def test_a_label_added_to_the_schema_cannot_be_silently_dropped() -> None:
    """The structural half of the fix: render order is DERIVED from SECTIONS, so a future label is
    rendered by construction rather than needing a second list to be remembered."""
    assert "SECTIONS" in continuation.render_packet.__globals__
    # every non-NEXT section renders; NEXT is deliberately emitted separately, after the task state
    out = render_packet({"notes": {s.lower(): "x" for s in SECTIONS}})
    assert out.count("NEXT:") == 1
