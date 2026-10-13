"""Sali knows who Almir is and what is happening in his life.

Before this existed: `sali.person` had 0 rows, `sali.graph_node` held 3187 tool nodes and 0 people,
and asked "i lost 2 of my legs and 1 hand" the five memories retrieved were a file_metadata failure,
a SaaS-website experience, an Opus-5 remark, a test artefact and "Python virtual environments here:
venv: sali (py 3.14.6)". Sali modelled his OWN state every turn and never Almir's.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sali.people.store import LifeEvent, render_life


def _e(said: str, at: datetime | None = None) -> LifeEvent:
    return LifeEvent(said=said, at=at or datetime(2026, 9, 7, tzinfo=UTC))


def test_it_is_his_words_not_a_summary() -> None:
    """The entry is the sentence he typed. Inferring is how this went wrong the first time —
    "she'll be back tomorrow evening, around 5 PM" was invented to fill exactly this gap."""
    out = render_life([_e("no sali, my wife left me")])
    assert '"no sali, my wife left me"' in out


def test_it_says_that_this_is_all_he_knows() -> None:
    """The store closes a fabrication gap, so it has to state its own boundary — otherwise it is one
    more place to reason confidently past the evidence."""
    assert "you know nothing beyond it" in render_life([_e("my wife left me")])


def test_every_entry_is_dated() -> None:
    """Undated, "my wife left me" is a permanent present tense Sali keeps reacting to as news."""
    out = render_life([_e("my wife left me")], ago=lambda _: "three days ago")
    assert "three days ago" in out


def test_elapsed_is_preferred_to_a_calendar_date() -> None:
    """Measuring a date against "now" is the arithmetic that produced "since last year (August
    2024)" about work done that same week."""
    assert "an hour ago" in render_life([_e("my wife left me")], ago=lambda _: "an hour ago")


def test_an_empty_life_costs_nothing() -> None:
    assert render_life([]) == ""


def test_the_render_is_bounded() -> None:
    """A life, not a transcript: the row keeps everything, the prompt carries what is current."""
    out = render_life([_e(f"thing {i}") for i in range(8)])
    assert out.count("he told you") == 8
