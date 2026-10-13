"""Valence per life event, and a capture switch that reaches the process doing the capturing."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sali.people.store import GOOD, HARD, NEUTRAL, LifeEvent, render_life, tone_of


@pytest.mark.parametrize("said,expected", [
    ("my brother is getting married next month", GOOD),
    ("my sister had a baby", GOOD),
    ("i got promoted today", GOOD),
    ("no sali, my wife left me", HARD),
    ("i got an accident and i lost 2 of my legs", HARD),
    ("my dad is in hospital", HARD),
    ("my cousin came over", NEUTRAL),
])
def test_tone(said: str, expected: str) -> None:
    assert tone_of(said) == expected


def test_the_hard_half_governs_a_mixed_sentence() -> None:
    assert tone_of("my brother got married but my father died") == HARD


def test_an_unrecognised_event_is_not_forced_into_a_bucket() -> None:
    """An unknown event described as "hard" is worse than one described as nothing — and guessing
    how Almir feels is the same fabrication this store exists to prevent, one layer down."""
    assert tone_of("i went to the shop") == NEUTRAL


def test_the_render_states_the_tone_per_event() -> None:
    """Without it, one hard thing colours everything after it: a wedding announced during a hard
    month was answered "It's okay, Sir. We'll get through whatever comes."."""
    out = render_life([
        LifeEvent("my wife left me", datetime(2026, 9, 1, tzinfo=UTC), HARD),
        LifeEvent("my brother is getting married", datetime(2026, 9, 7, tzinfo=UTC), GOOD),
    ])
    assert "[hard]" in out and "[good news]" in out


def test_a_neutral_event_is_unmarked() -> None:
    out = render_life([LifeEvent("my cousin came over", datetime(2026, 9, 7, tzinfo=UTC), NEUTRAL)])
    assert "[" not in out


def test_the_capture_switch_is_visible_across_processes(tmp_path, monkeypatch) -> None:
    """An environment variable was the obvious switch and did NOT work: `sali agent` attaches to the
    running daemon, so the variable was set in the wrong process and two memories were still
    written. Both mechanisms are honoured now."""
    from sali.runtime import loop as loop_mod

    monkeypatch.delenv("SALI_NO_CAPTURE", raising=False)
    monkeypatch.setattr("sali.db.reset.STATE_DIR", tmp_path)
    assert not loop_mod._capture_suppressed()

    (tmp_path / "no-capture").write_text("x")
    assert loop_mod._capture_suppressed(), "the daemon must see a sentinel written by another process"

    (tmp_path / "no-capture").unlink()
    assert not loop_mod._capture_suppressed()

    monkeypatch.setenv("SALI_NO_CAPTURE", "1")
    assert loop_mod._capture_suppressed(), "still honoured when this terminal IS the Sali"
