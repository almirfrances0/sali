"""Permission policy, redaction, and the tool contract (spec test 8, partial)."""

from __future__ import annotations

from typing import Any

from sali.core.enums import Capability, RiskLevel
from sali.security.policy import Action, PolicyEngine
from sali.security.redact import looks_like_secret, redact, redact_obj
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


def test_ordinary_network_tool_auto_allows() -> None:
    # Everyday work runs free (the freedom policy) — no per-session grant machinery needed.
    assert PolicyEngine().decide(_NetworkTool(), {}).action is Action.AUTO_ALLOW


def test_allowlist_mode_denies_unlisted() -> None:
    engine = PolicyEngine(allowlist=frozenset({"some_other_tool"}))
    assert engine.decide(MemoryInfo(), {}).action is Action.DENY


def test_redaction_scrubs_secrets() -> None:
    assert "postgresql://" not in redact("dsn=postgresql://u:pw@localhost/sali")
    assert "almir@example.com" not in redact("contact almir@example.com please")
    assert redact_obj({"password": "hunter2", "note": "ok"})["password"] == "[redacted]"


def test_redaction_scrubs_inline_cli_credentials() -> None:
    # Passwords passed on a command line (e.g. surfaced via a terminal's window title).
    assert "hunter2" not in redact("mysql -phunter2 sali")
    assert "s3cr3t" not in redact("redis-cli -a s3cr3t")
    assert "p4ss" not in redact("curl -u admin:p4ss https://ex.com")
    assert "topsecret" not in redact("pg_dump --password=topsecret db")


def test_redaction_leaves_benign_attached_flags_alone() -> None:
    # Command-anchored patterns must NOT eat innocent flags — those stay legible in logs.
    for benign in ("tar -pxzf archive.tar", "mkdir -p /tmp/sali", "ps aux -p 1234", "grep -n foo"):
        assert redact(benign) == benign


def test_looks_like_secret_detects_and_ignores() -> None:
    assert looks_like_secret("token=ghp_" + "a" * 30)
    assert looks_like_secret("reach me at almir@example.com")
    assert looks_like_secret("mysql -phunter2 db")
    assert not looks_like_secret("just an ordinary note about the project")
    assert not looks_like_secret("mkdir -p /tmp/sali")
