"""The epistemic authority hierarchy for Sali's own claims (§36-38).

When two sources disagree about what is true — a live check of the world, Sali's durable state, a
recalled memory, or the model's own guess — one of them has to win, and it must be the SAME one every
time or "grounded" stops meaning anything. This module names that order once, as data, so a snapshot
can label where each fact came from and a caller resolving a conflict can pick the winner by rule
rather than by vibe. It is the ranking; the response-claim validator (verify/response_claims) is the
place that ENFORCES it on outgoing wording — the two agree by construction because both point here.

The order, highest first:
  OBSERVED — something checked against the world THIS turn: a tool receipt proving an effect, a live
             machine/filesystem probe, the real transport a request arrived on. Reality, freshly seen.
  STATE    — Sali's durable structured state: the sali_state row, the agent:sali identity, commitment
             and task rows. Written by past reality; survives restarts. Beats memory and guesses.
  RECALL   — a recalled memory (semantic/episodic). True once; may be stale now.
  BELIEF   — the model's prior: a plausible guess with no backing. Lowest; never grounds a claim.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any


class ClaimAuthority(IntEnum):
    """How much a fact's SOURCE lets Sali trust it. Higher wins when sources disagree."""

    BELIEF = 1
    RECALL = 2
    STATE = 3
    OBSERVED = 4

    @property
    def label(self) -> str:
        return self.name.lower()


def resolve(candidates: list[tuple[ClaimAuthority, Any]]) -> tuple[ClaimAuthority, Any] | None:
    """Given (authority, value) candidates for the SAME fact, return the highest-authority one whose
    value is not None, or None if there is nothing to go on. A caller lists its best source first, so
    an equal-authority tie keeps the first — but the point of the hierarchy is that ties are rare."""
    best: tuple[ClaimAuthority, Any] | None = None
    for tier, value in candidates or []:
        if value is None:
            continue
        if best is None or tier > best[0]:
            best = (tier, value)
    return best


# The order as data, highest first — for a snapshot header and for anyone rendering the hierarchy.
ORDER: tuple[ClaimAuthority, ...] = (
    ClaimAuthority.OBSERVED, ClaimAuthority.STATE, ClaimAuthority.RECALL, ClaimAuthority.BELIEF)

PRECEDENCE_LINE = (
    "what I just observed or ran this turn > my durable state > memory > a guess"
)

__all__ = ["ClaimAuthority", "resolve", "ORDER", "PRECEDENCE_LINE"]
