"""The prompt names the part of day instead of leaving the model to infer it.

Sali answered "now" with "It's Monday morning here (14:48 EAT)" — date, weekday and digits all
correct, and the one word he had to infer wrong. A Q2_K model reading "14:48" does not reliably
reach "afternoon".
"""
from __future__ import annotations

import pytest

from sali.core.temporal import _DAY_PARTS, TemporalService, part_of_day


@pytest.mark.parametrize("hour,expected", [
    (0, "night"), (2, "night"), (4, "night"),
    (5, "morning"), (9, "morning"), (11, "morning"),
    (12, "afternoon"), (14, "afternoon"), (16, "afternoon"),
    (17, "evening"), (21, "evening"),
    (22, "night"), (23, "night"),
])
def test_every_hour_gets_the_ordinary_word(hour: int, expected: str) -> None:
    assert part_of_day(hour) == expected


def test_1448_is_not_the_morning() -> None:
    """The exact failure, kept as its own case so it can never come back quietly."""
    assert part_of_day(14) == "afternoon"


def test_the_day_is_partitioned_with_no_gap() -> None:
    assert {part_of_day(h) for h in range(24)} == {"morning", "afternoon", "evening", "night"}


def test_the_rendered_word_agrees_with_the_parser() -> None:
    """Rendering and parsing read the same table, so the word Sali is handed always falls inside the
    window he would resolve that word back to."""
    for name in ("morning", "afternoon", "evening"):
        start, end = _DAY_PARTS[name]
        for hour in range(start, end):
            assert part_of_day(hour) == name, f"{hour}:00 rendered as {part_of_day(hour)}, parses as {name}"


def test_the_header_carries_it() -> None:
    block = TemporalService().context_block()
    assert any(w in block for w in ("morning", "afternoon", "evening", "night")), block
