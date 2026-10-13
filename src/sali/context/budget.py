"""Token-budget packing.

Sections carry a priority. P0 (identity, security) is never cut. Everything else is filled
in priority-then-score order until the budget is reached; the rest is dropped and reported,
never silently truncated (fix H7 / spec §13). The total never exceeds the budget except for
the P0 floor, which is tiny by construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class Priority(IntEnum):
    P0 = 0  # identity, security — never cut
    P1 = 1  # live-state note, tools menu
    P2 = 2  # retrieved memories, graph facts
    P3 = 3  # recent activity


@dataclass(slots=True)
class Section:
    name: str
    priority: Priority
    text: str
    tokens: int
    score: float = 0.0


@dataclass(slots=True)
class PackResult:
    sections: list[Section]
    dropped: list[str]
    total_tokens: int


def pack(sections: list[Section], budget: int, *, mandatory_tokens: int = 0) -> PackResult:
    """Include every P0 section, then greedily add the rest by (priority, -score)."""
    included = [s for s in sections if s.priority is Priority.P0]
    used = mandatory_tokens + sum(s.tokens for s in included)

    rest = sorted(
        (s for s in sections if s.priority is not Priority.P0),
        key=lambda s: (int(s.priority), -s.score),
    )
    dropped: list[str] = []
    for section in rest:
        if used + section.tokens <= budget:
            included.append(section)
            used += section.tokens
        else:
            dropped.append(section.name)

    included.sort(key=lambda s: int(s.priority))
    return PackResult(sections=included, dropped=dropped, total_tokens=used)
