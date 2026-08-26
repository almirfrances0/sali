"""execute_command — Sali runs commands on its own machine, freely.

This is Sali's home, so ordinary commands (checking things, installing tools, poking around)
run without asking. The tool only *escalates itself* to a confirmation when the command is
genuinely destructive (rm -rf, mkfs, dd to a device, a fork bomb, …) — the policy then pauses
so Sali (and Almir) think first, the way anyone would before wrecking their own system.

For risky experiments there's ``sandbox=true``: the command runs inside a throwaway bubblewrap
jail so installing/deleting can't touch the real system (used during learning).
"""

from __future__ import annotations

import os
import re
from typing import Any

from sali.core.enums import Capability, RiskLevel
from sali.tools import jail
from sali.tools.base import Tool, ToolResult
from sali.tools.context import ToolContext
from sali.tools.exec import CommandTimeout, run_argv
from sali.tools.registry import ToolRegistry

_MAX = 64 * 1024

# Commands that destroy data/hardware/the running system → self-escalate to R4 (confirm).
_DESTRUCTIVE = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\brm\b(?=.*\s-\w*[rf])",  # rm -r / -f
        r"\brm\b\s+(-\w+\s+)*(/|~|\*)",  # rm targeting root / home / a glob
        r"\b(mkfs|mke2fs|fdisk|parted|sgdisk|wipefs|shred|blkdiscard|cryptsetup|mkswap)\b",
        r"\bdd\b.*\bof=/dev/",
        r">\s*/dev/(sd|nvme|hd|mmcblk|vd)",
        r"\b(shutdown|reboot|halt|poweroff)\b",
        r"\b(userdel|deluser|groupdel)\b",
        r"\bchmod\b\s+-\w*R\s+0*0\b",
        r":\(\)\s*\{\s*:\s*\|\s*:",  # fork bomb
        r"\bmv\b.*\s/dev/null\b",
        r"\btruncate\b\s+-s\s*0",
    )
]
_SECRET_ENV = re.compile(r"(?i)(key|secret|token|password|passwd|credential|\bapi\b)")


def _as_text(command: Any) -> str:
    if isinstance(command, str):
        return command
    if isinstance(command, list):
        return " ".join(str(c) for c in command)
    return ""


def _is_destructive(command: Any) -> bool:
    text = _as_text(command)
    return any(pattern.search(text) for pattern in _DESTRUCTIVE)


def _safe_env() -> dict[str, str]:
    # The real environment (so PATH/HOME/tooling work), minus obviously secret-shaped vars —
    # Sali runs freely, but I don't hand credentials to arbitrary commands.
    return {k: v for k, v in os.environ.items() if not _SECRET_ENV.search(k)}


class ExecuteCommand(Tool):
    name = "execute_command"
    description = (
        "Run any shell command on your own machine — check things, install tools, whatever you "
        "need. Pass sandbox=true to run it in a throwaway isolated environment for risky tests."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}],
                "description": "A shell command string, or an argv list.",
            },
            "sandbox": {"type": "boolean", "description": "Run isolated (learning experiments)."},
        },
        "required": ["command"],
    }
    risk_level = RiskLevel.R1  # ordinary; assess() escalates destructive commands
    capabilities = frozenset({Capability.EXECUTE, Capability.NETWORK})
    idempotent = False
    timeout_s = 120.0

    def assess(self, args: dict[str, Any]) -> RiskLevel:
        return RiskLevel.R4 if _is_destructive(args.get("command")) else RiskLevel.R1

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = args.get("command")
        if isinstance(command, str):
            argv = ["bash", "-c", command]
        elif isinstance(command, list) and command and all(isinstance(c, str) for c in command):
            argv = command
        else:
            return ToolResult(ok=False, display="bad command", error="command must be a string or argv list")

        perms = ctx.settings.permissions
        sandbox = bool(args.get("sandbox"))
        if sandbox:
            if not (perms.jail_learning and jail.available()):
                return ToolResult(ok=False, display="no sandbox",
                                  error="the learning sandbox needs bubblewrap")
            # Fake-root, writable-ephemeral system: install/delete freely, host untouched.
            argv = jail.build_sandbox_argv(argv, allow_network=perms.exec_allow_network)

        try:
            rc, out, err = await run_argv(
                argv, timeout=self.timeout_s, env=_safe_env(), max_output=256 * 1024
            )
        except CommandTimeout as exc:
            return ToolResult(ok=False, display="timeout", error=str(exc))
        except FileNotFoundError as exc:
            return ToolResult(ok=False, display="not found", error=str(exc))

        return ToolResult(
            ok=(rc == 0),
            output={"returncode": rc, "stdout": out[:_MAX], "stderr": err[:_MAX], "sandboxed": sandbox},
            display=f"exit {rc}" + (" (sandboxed)" if sandbox else ""),
            error=None if rc == 0 else (err.strip()[:300] or f"exit {rc}"),
        )


def register_builtins(registry: ToolRegistry) -> None:
    registry.register(ExecuteCommand())
