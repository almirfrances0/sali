"""The Attention Engine (spec §14/§15) — "does this matter, and what should I do about it?".

The single most important piece of a persistent Sali (the spec's own emphasis): cheap events flow
continuously, the model stays warm, and this deterministic layer decides — with NO model — which
observations are worth recording, which warrant waking reasoning, and which deserve telling Almir
promptly. Without it, "always running" is just an expensive model idling in VRAM.

Two decisions, both pure/testable:
  * TIER   — routine | interesting | important | critical (from importance + categorical signals)
  * ACTION — ignore | record | investigate | notify

`investigate` is the only action that may wake the model, and only for a high tier carrying a signal
that genuinely needs interpretation — so attention protects the inference budget (§79), it doesn't
spend it. Everything here is a judgement about whether to think, made cheaply before thinking.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AttentionTier(StrEnum):
    ROUTINE = "routine"          # background churn — not worth attention
    INTERESTING = "interesting"  # worth remembering, not worth interrupting
    IMPORTANT = "important"      # worth Sali's attention; may surface
    CRITICAL = "critical"        # worth telling Almir promptly


class AttentionAction(StrEnum):
    IGNORE = "ignore"            # drop it
    RECORD = "record"           # persist as an observation (no model)
    INVESTIGATE = "investigate"  # wake reasoning to interpret it (rare, budget-gated)
    NOTIFY = "notify"           # surface to Almir proactively (§16)


@dataclass(slots=True)
class Verdict:
    tier: AttentionTier
    action: AttentionAction
    reason: str


# Categorical signals that escalate a tier regardless of the raw score. Populated by the observation
# sources (fs/window now; process/service/network/security events as the event bus grows — §13).
_CRITICAL_SIGNALS = frozenset({"disk_full", "service_failed", "security_exposure", "secret_leak"})
_IMPORTANT_SIGNALS = frozenset({"new_service", "recurring_failure", "config_change", "source_delete",
                                "unknown_process"})
# Signals that (at a high tier) genuinely need model interpretation rather than a fixed response.
_INVESTIGATE_SIGNALS = frozenset({"new_service", "recurring_failure", "unknown_process"})

_TIER_ACTION = {
    AttentionTier.ROUTINE: AttentionAction.IGNORE,
    AttentionTier.INTERESTING: AttentionAction.RECORD,
    AttentionTier.IMPORTANT: AttentionAction.RECORD,
    AttentionTier.CRITICAL: AttentionAction.NOTIFY,
}


def _tier(importance: float, signals: frozenset[str], count: int) -> AttentionTier:
    if signals & _CRITICAL_SIGNALS or importance >= 0.85:
        return AttentionTier.CRITICAL
    if signals & _IMPORTANT_SIGNALS or importance >= 0.70:
        return AttentionTier.IMPORTANT
    if importance >= 0.55 or count >= 25:
        return AttentionTier.INTERESTING
    return AttentionTier.ROUTINE


def assess(*, importance: float, signals: frozenset[str] = frozenset(), count: int = 1) -> Verdict:
    """Decide the attention tier + action for one observation. Deterministic; no I/O, no model."""
    sig = frozenset(signals)
    tier = _tier(importance, sig, count)
    action = _TIER_ACTION[tier]
    # Only a high-tier observation carrying an interpretation-needing signal wakes the model.
    if tier in (AttentionTier.IMPORTANT, AttentionTier.CRITICAL) and (sig & _INVESTIGATE_SIGNALS):
        action = AttentionAction.INVESTIGATE
    reason = f"tier={tier.value}" + (f", signals={sorted(sig)}" if sig else "")
    return Verdict(tier=tier, action=action, reason=reason)
