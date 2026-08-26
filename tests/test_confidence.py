"""Confidence model — evidence-derived, never blind (engineering rule 2). Spec test 5."""

from __future__ import annotations

from sali.core.enums import MemorySource
from sali.memory.confidence import apply_evidence, bump_reliability, initial_confidence


def test_confidence_never_reaches_certainty() -> None:
    assert initial_confidence(MemorySource.SYSTEM_OBSERVATION) <= 0.99


def test_observation_outweighs_inference() -> None:
    d_obs = initial_confidence(MemorySource.SYSTEM_OBSERVATION) - 0.5
    d_inf = initial_confidence(MemorySource.INFERENCE) - 0.5
    assert d_obs > d_inf > 0.0


def test_zero_confidence_observation_does_not_move() -> None:
    assert abs(apply_evidence(0.5, MemorySource.INFERENCE, obs_conf=0.0) - 0.5) < 1e-9


def test_corroboration_increases_but_saturates() -> None:
    c1 = initial_confidence(MemorySource.TOOL_RESULT)
    c2 = apply_evidence(c1, MemorySource.TOOL_RESULT)
    c3 = apply_evidence(c2, MemorySource.TOOL_RESULT)
    assert c1 < c2 < c3 < 1.0
    assert (c2 - c1) > (c3 - c2)  # diminishing returns


def test_reliability_tracks_strongest_source() -> None:
    r = bump_reliability(0.0, MemorySource.INFERENCE)
    r = bump_reliability(r, MemorySource.SYSTEM_OBSERVATION)
    r = bump_reliability(r, MemorySource.CONVERSATION)
    assert r == 1.0  # a direct observation once supported it
