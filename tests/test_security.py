"""Permission policy, redaction, and the tool contract (spec test 8, partial)."""

from __future__ import annotations

from typing import Any

from sali.core.enums import Capability, RiskLevel
from sali.security.policy import Action, PolicyEngine, SessionGrants
from sali.security.redact import redact, redact_obj
from sali.tools.base import Tool, ToolResult
from sali.tools.builtins.system import MemoryInfo


class _Destructive(Tool):
    name = "wipe_everything"
    description = "test-only"
    risk_level = RiskLevel.R4
    capabilities = frozenset({Capability.DESTRUCTIVE})

    async def run(self, args: dict[str, Any], ctx: Any) -> ToolResult:
        return ToolResult(ok=True, output={"done": True})


class _LowRiskDestructive(Tool):
    name = "delete_one"
    description = "test-only"
    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.DESTRUCTIVE})

    async def run(self, args: dict[str, Any], ctx: Any) -> ToolResult:
        return ToolResult(ok=True, output={})


class _NetworkTool(Tool):
    name = "web_fetch"
    description = "test-only"
    risk_level = RiskLevel.R2
    capabilities = frozenset({Capability.NETWORK})

    async def run(self, args: dict[str, Any], ctx: Any) -> ToolResult:
        return ToolResult(ok=True, output={})


def test_r0_auto_allow() -> None:
    assert PolicyEngine().decide(MemoryInfo(), {}).action is Action.AUTO_ALLOW


def test_r4_confirms_not_denies() -> None:
    # In Sali's home nothing is auto-denied; a destructive R4 tool pauses to confirm.
    assert PolicyEngine().decide(_Destructive(), {}).action is Action.CONFIRM


def test_denylist_beats_everything() -> None:
    engine = PolicyEngine(denylist=frozenset({"memory_info"}))
    assert engine.decide(MemoryInfo(), {}).action is Action.DENY


def test_destructive_capability_forces_confirm() -> None:
    # An otherwise-low-risk tool that is DESTRUCTIVE must not auto-run.
    assert PolicyEngine().decide(_LowRiskDestructive(), {}).action is Action.CONFIRM


def test_session_grant_upgrades_confirm_to_auto() -> None:
    # A grant upgrades a non-destructive CONFIRM tool...
    grants = SessionGrants(allowed={"web_fetch"})
    assert PolicyEngine().decide(_NetworkTool(), {}, grants).action is Action.AUTO_ALLOW


def test_session_grant_refused_for_destructive() -> None:
    # ...but a name-only grant must NOT auto-allow a destructive/high-risk tool.
    grants = SessionGrants(allowed={"delete_one"})
    assert PolicyEngine().decide(_LowRiskDestructive(), {}, grants).action is Action.CONFIRM


def test_allowlist_mode_denies_unlisted() -> None:
    engine = PolicyEngine(allowlist=frozenset({"some_other_tool"}))
    assert engine.decide(MemoryInfo(), {}).action is Action.DENY


def test_redaction_scrubs_secrets() -> None:
    assert "postgresql://" not in redact("dsn=postgresql://u:pw@localhost/sali")
    assert "almir@example.com" not in redact("contact almir@example.com please")
    assert redact_obj({"password": "hunter2", "note": "ok"})["password"] == "[redacted]"
