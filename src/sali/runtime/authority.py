"""Authority model (§52/§53) — structured authority context for the reasoning engine.

The critical separation Prompt 10 must preserve: capability ≠ authority ≠ intent ≠ action. Having the
technical means to send an email, delete a file, or create an account does NOT mean Sali should. This
module answers, deterministically, "under what authority (if any) may this action run autonomously?" —
it provides a small, contextual authority LADDER (not hundreds of hardcoded rules), and hands the
reasoning engine a structured decision. The natural-language way Sali *communicates* a
requires-human decision is Prompt 11's job; here we only produce the structured state (§51).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class AuthorityLevel(StrEnum):
    EXPLICITLY_REQUESTED = "explicitly_requested"        # the user asked for exactly this
    STANDING_AUTHORIZATION = "standing_authorization"    # the user granted a standing permission
    TASK_SCOPED = "task_scoped"                           # within the current task's authority
    COMMITMENT_SCOPED = "commitment_scoped"              # within a commitment's allowed actions
    SAFE_AUTONOMOUS = "safe_autonomous"                  # reversible + non-consequential (observe/research)
    REQUIRES_HUMAN = "requires_human"                    # needs a human decision before acting
    NOT_AUTHORIZED = "not_authorized"                    # explicitly disallowed


# The levels at which Sali may proceed WITHOUT pausing for a human decision.
_AUTONOMOUS = frozenset((AuthorityLevel.EXPLICITLY_REQUESTED, AuthorityLevel.STANDING_AUTHORIZATION,
                         AuthorityLevel.TASK_SCOPED, AuthorityLevel.COMMITMENT_SCOPED,
                         AuthorityLevel.SAFE_AUTONOMOUS))


@dataclass(slots=True)
class AuthorityDecision:
    level: AuthorityLevel
    allowed_autonomously: bool
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {"level": self.level.value, "allowed_autonomously": self.allowed_autonomously,
                "reasons": self.reasons}


def classify_action(
    *, consequential: bool, reversible: bool, explicitly_requested: bool = False,
    standing_authorization: bool = False, has_task_authority: bool = False,
    has_commitment_authority: bool = False, forbidden: bool = False,
) -> AuthorityDecision:
    """Classify an action's authority from its context (§52). Deterministic ladder, most-specific first.
    NOTE: this does NOT consider whether Sali has the *capability* — capability is a separate axis (§53);
    an action can be fully authorized yet impossible, or possible yet requiring a human decision."""
    if forbidden:
        return AuthorityDecision(AuthorityLevel.NOT_AUTHORIZED, False, ["explicitly disallowed"])
    if explicitly_requested:
        return AuthorityDecision(AuthorityLevel.EXPLICITLY_REQUESTED, True, ["user requested this action"])
    if standing_authorization:
        return AuthorityDecision(AuthorityLevel.STANDING_AUTHORIZATION, True, ["standing permission granted"])
    # An irreversible, consequential action always needs a human decision, whatever scope exists (§72).
    if consequential and not reversible:
        return AuthorityDecision(AuthorityLevel.REQUIRES_HUMAN, False,
                                 ["consequential", "not reversible"])
    if has_task_authority and (reversible or not consequential):
        return AuthorityDecision(AuthorityLevel.TASK_SCOPED, True, ["within the current task's authority"])
    if has_commitment_authority and reversible:
        return AuthorityDecision(AuthorityLevel.COMMITMENT_SCOPED, True,
                                 ["within a commitment's allowed actions"])
    if not consequential and reversible:
        return AuthorityDecision(AuthorityLevel.SAFE_AUTONOMOUS, True, ["reversible", "non-consequential"])
    # consequential-but-reversible without a scope, or anything else → ask the human (§50/§51)
    return AuthorityDecision(AuthorityLevel.REQUIRES_HUMAN, False,
                             ["consequential without sufficient authority"])


def is_autonomous(level: AuthorityLevel) -> bool:
    """Whether an action at this authority level may proceed without pausing for the user."""
    return level in _AUTONOMOUS
