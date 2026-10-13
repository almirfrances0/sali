"""The confidence model.

Confidence is evidence-derived, never blindly set (engineering rule 2). Each supporting
observation nudges confidence via a log-odds update weighted by the source's evidence
priority, saturating in probability space so corroboration diminishes and a bare ``guess``
(priority 0) barely moves the needle — and it is clamped below 1.0, so nothing is ever
"certain".
"""

from __future__ import annotations

import math

from sali.core.enums import MemorySource, source_priority

ALPHA = 1.5
_LO, _HI = 0.01, 0.99


def _logit(p: float) -> float:
    p = min(max(p, _LO), _HI)
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def evidence_weight(source: MemorySource, obs_conf: float = 1.0) -> float:
    """0.0 for a guess, 1.0 for a direct system observation (scaled by observation confidence)."""
    return (source_priority(source) / 100.0) * obs_conf


def apply_evidence(prior: float, source: MemorySource, obs_conf: float = 1.0) -> float:
    """Fold one supporting observation into a prior probability; clamped to [0.01, 0.99]."""
    updated = _sigmoid(_logit(prior) + ALPHA * evidence_weight(source, obs_conf))
    return min(max(updated, _LO), _HI)


def initial_confidence(source: MemorySource, obs_conf: float = 1.0) -> float:
    """Confidence for a brand-new memory: one observation folded into a 0.5 prior."""
    return apply_evidence(0.5, source, obs_conf)


def bump_reliability(current: float, source: MemorySource) -> float:
    """Reliability tracks the strongest source that ever supported the memory."""
    return max(current, source_priority(source) / 100.0)
