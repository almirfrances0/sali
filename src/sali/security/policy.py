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


_RISK_ACTION: dict[RiskLevel, Action] = {
    RiskLevel.R0: Action.AUTO_ALLOW,
    RiskLevel.R1: Action.AUTO_ALLOW,
    RiskLevel.R2: Action.CONFIRM,
    RiskLevel.R3: Action.CONFIRM,
    RiskLevel.R4: Action.DENY,
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
        if tool.name in self.denylist:  # hard denylist always wins
            return PolicyDecision(Action.DENY, f"{tool.name} is denylisted", tool.risk_level)
        if not tool.available:
            return PolicyDecision(Action.DENY, f"{tool.name} is unavailable", tool.risk_level)
        if self.allowlist and tool.name not in self.allowlist:  # allowlist mode: default-deny
            return PolicyDecision(Action.DENY, f"{tool.name} is not on the allowlist", tool.risk_level)

        risk = tool.risk_level
        # A DESTRUCTIVE capability forces at least a confirmation, regardless of nominal risk.
        if Capability.DESTRUCTIVE in tool.capabilities and risk < RiskLevel.R2:
            risk = RiskLevel.R2

        action = _RISK_ACTION[risk]
        # A per-session grant may upgrade a CONFIRM to AUTO_ALLOW — but never for a destructive
        # or high-risk (R3+) tool: a name-only grant must not silently authorize those.
        grantable = risk < RiskLevel.R3 and Capability.DESTRUCTIVE not in tool.capabilities
        if grants is not None and tool.name in grants.allowed and action is Action.CONFIRM and grantable:
            return PolicyDecision(Action.AUTO_ALLOW, "granted for this session", risk)
        reason = {
            Action.AUTO_ALLOW: "read-only / low-risk",
            Action.CONFIRM: "requires confirmation",
            Action.DENY: "destructive — denied by default",
        }[action]
        return PolicyDecision(action, reason, risk)
