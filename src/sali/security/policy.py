"""The permission policy.

Maps a tool's risk level to an action — auto-allow / confirm / deny — with a hard denylist
that always wins, a DESTRUCTIVE-capability floor, per-session sticky grants, and R4 treated
as deny-by-default. The *decision* is made here (server-side); how a confirmation is
collected is a separate concern (see ``confirm.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from sali.core.enums import Capability, RiskLevel
from sali.tools.base import Tool


class Action(StrEnum):
    AUTO_ALLOW = "auto_allow"
    CONFIRM = "confirm"
    DENY = "deny"


@dataclass(slots=True)
class PolicyDecision:
    action: Action
    reason: str
    risk: RiskLevel


# Sali's home: ordinary work runs free; only genuinely destructive acts (R4) pause to confirm.
# Nothing is auto-denied except an explicit denylist — this is a resident with judgment, not a
# gated agent.
_RISK_ACTION: dict[RiskLevel, Action] = {
    RiskLevel.R0: Action.AUTO_ALLOW,
    RiskLevel.R1: Action.AUTO_ALLOW,
    RiskLevel.R2: Action.AUTO_ALLOW,
    RiskLevel.R3: Action.AUTO_ALLOW,
    RiskLevel.R4: Action.CONFIRM,
}


@dataclass(slots=True)
class SessionGrants:
    """Sticky per-session approvals (a user can 'allow this tool for now')."""

    allowed: set[str] = field(default_factory=set)


class PolicyEngine:
    def __init__(
        self,
        *,
        denylist: frozenset[str] = frozenset(),
        allowlist: frozenset[str] = frozenset(),
    ) -> None:
        self.denylist = denylist
        self.allowlist = allowlist

    def decide(
        self, tool: Tool, args: dict[str, object], grants: SessionGrants | None = None
    ) -> PolicyDecision:
        if tool.name in self.denylist:  # explicit hard denylist (rarely used)
            return PolicyDecision(Action.DENY, f"{tool.name} is denylisted", tool.risk_level)
        if not tool.available:
            return PolicyDecision(Action.DENY, f"{tool.name} is unavailable", tool.risk_level)
        if self.allowlist and tool.name not in self.allowlist:  # allowlist mode (opt-in)
            return PolicyDecision(Action.DENY, f"{tool.name} is not on the allowlist", tool.risk_level)

        risk = tool.assess(args)  # per-call risk: a destructive command escalates itself
        action = _RISK_ACTION[risk]
        # A DESTRUCTIVE-capability tool always pauses at least for a nod, even if nominally low.
        if Capability.DESTRUCTIVE in tool.capabilities and action is Action.AUTO_ALLOW:
            action = Action.CONFIRM
        reason = {
            Action.AUTO_ALLOW: "ordinary work on Sali's own machine",
            Action.CONFIRM: "destructive — pausing to think first",
            Action.DENY: "blocked",
        }[action]
        return PolicyDecision(action, reason, risk)
