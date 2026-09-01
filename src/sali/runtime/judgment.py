"""Judgment / consequence model (§7/§8/§36/§73/§75) — decide how much care an action warrants.

This is NOT a giant hardcoded rule engine and NOT a y/n gate. It reasons over STRUCTURED dimensions of a
proposed action — reversibility, scope, externality, whether it's public/financial/destructive, and the
ambiguity of its target — combined with the authority context (Prompt 10), and produces a deterministic
JUDGMENT LEVEL (low → blocked). The level is a decision SIGNAL, not a prohibition (§8): most work is
low/normal and proceeds; genuinely consequential actions ask for natural consent; only a real
policy/security/capability boundary is BLOCKED. A new capability plugs in by describing its actions
(CapabilityAction) — the judgment layer never needs per-application knowledge (§74/§75).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from sali.runtime.authority import AuthorityDecision, AuthorityLevel, classify_action


class JudgmentLevel(StrEnum):
    LOW = "low"                          # read/inspect — proceed
    NORMAL = "normal"                    # ordinary task work within authority — proceed
    ATTENTION = "attention"              # ambiguous or unexpected — explain what was found
    CONSENT = "consent"                  # consequential — ask naturally before proceeding
    HIGH_CONSEQUENCE = "high_consequence"  # unusually consequential — explain what/why/scope + ask
    BLOCKED = "blocked"                  # genuinely disallowed by policy/security/capability/authority


@dataclass(slots=True)
class CapabilityAction:
    """A capability-metadata descriptor of a proposed action (§75). The judgment layer operates on this,
    not on hardcoded knowledge of any particular application — so future capabilities plug in for free."""
    capability: str
    action: str
    target: str | None = None
    scope: str = "local"                 # local | project | machine | external | public
    reversible: bool = True
    external: bool = False
    public: bool = False
    financial: bool = False
    destructive: bool = False
    ambiguous_target: bool = False


@dataclass(slots=True)
class JudgmentDecision:
    level: JudgmentLevel
    needs_consent: bool
    authority: AuthorityDecision
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {"level": self.level.value, "needs_consent": self.needs_consent,
                "authority": self.authority.to_dict(), "reasons": self.reasons}


def assess(
    act: CapabilityAction, *, capability_available: bool = True, forbidden: bool = False,
    explicitly_requested: bool = False, standing_authorization: bool = False,
    has_task_authority: bool = False, has_commitment_authority: bool = False,
) -> JudgmentDecision:
    """Assess a proposed action. Deterministic. Combines the consequence dimensions with the authority
    ladder (§8/§53): capability, authority, and consequence are separate axes — an action can be fully
    authorized yet consequential, or possible yet lacking authority. Returns the judgment + whether
    natural consent is required now (suppressed when the user already authorized it, §9/§13)."""
    reasons: list[str] = []
    authority = classify_action(
        consequential=_is_consequential(act), reversible=act.reversible, forbidden=forbidden,
        explicitly_requested=explicitly_requested, standing_authorization=standing_authorization,
        has_task_authority=has_task_authority, has_commitment_authority=has_commitment_authority)

    # A genuine boundary — policy, or a capability Sali doesn't actually have (§8/§28/§56).
    if forbidden or authority.level is AuthorityLevel.NOT_AUTHORIZED:
        return JudgmentDecision(JudgmentLevel.BLOCKED, False, authority, ["policy boundary"])
    if not capability_available:
        return JudgmentDecision(JudgmentLevel.BLOCKED, False, authority,
                                ["capability not available"])

    # Consequence level from the structured dimensions (§7).
    if act.destructive and not act.reversible:
        level, reasons = JudgmentLevel.HIGH_CONSEQUENCE, ["destructive", "irreversible"]
    elif act.financial or (act.external and act.public):
        level, reasons = JudgmentLevel.HIGH_CONSEQUENCE, (
            ["financial"] if act.financial else ["external", "public"])
    elif act.external or act.destructive or not act.reversible:
        level = JudgmentLevel.CONSENT
        reasons = [d for d, on in (("external", act.external), ("destructive", act.destructive),
                                   ("irreversible", not act.reversible)) if on]
    elif act.ambiguous_target:
        level, reasons = JudgmentLevel.ATTENTION, ["ambiguous target"]
    elif act.scope in ("machine",):
        level, reasons = JudgmentLevel.ATTENTION, ["machine-wide scope"]
    else:
        level = JudgmentLevel.NORMAL if _is_consequential(act) else JudgmentLevel.LOW
        reasons = ["within ordinary work"]

    # The user may have already authorized this — suppress redundant consent (§9/§10/§13).
    already_authorized = authority.level in (
        AuthorityLevel.EXPLICITLY_REQUESTED, AuthorityLevel.STANDING_AUTHORIZATION)
    needs_consent = level in (JudgmentLevel.CONSENT, JudgmentLevel.HIGH_CONSEQUENCE) \
        and not already_authorized
    if already_authorized and needs_consent is False and level in (
            JudgmentLevel.CONSENT, JudgmentLevel.HIGH_CONSEQUENCE):
        reasons.append("already authorized by the user")
    return JudgmentDecision(level, needs_consent, authority, reasons)


def _is_consequential(act: CapabilityAction) -> bool:
    """An action is consequential if it mutates the world in a way worth reasoning about (§7)."""
    return bool(act.destructive or act.external or act.public or act.financial
                or not act.reversible or act.scope in ("machine", "external", "public"))
