"""execute_command — the one arbitrary-binary tool, made safe by construction (fix H2).

Defence in depth, all of which must pass:
  1. **Allowlist only** — argv[0]'s basename must be in ``permissions.exec_allowlist``.
  2. **No shells/interpreters** — refused even if somehow allowlisted, so there is no path to
     arbitrary code (`bash -c`, `python -c`, `find -exec`, …).
  3. **argv, never a shell** — the command is a list; nothing is ever string-parsed.
  4. **Jailed** — run inside bubblewrap: read-only binds, private /proc + /tmp, no network by
     default, no access to anything not explicitly bound. If the jail is required but
     unavailable, the tool refuses rather than running unconfined.
  5. **Scrubbed environment** and a hard timeout that kills the process group.
It is R3 (confirmation with a typed phrase) on top of all this.
"""

from __future__ import annotations

from typing import Any

from sali.core.enums import Capability, RiskLevel
from sali.tools import jail
from sali.tools.base import Tool, ToolResult
from sali.tools.context import ToolContext
from sali.tools.exec import CommandTimeout, run_argv
from sali.tools.registry import ToolRegistry

_MAX = 64 * 1024
# Anything that can execute further code from its arguments is refused outright.
_INTERPRETERS = frozenset({
    "sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh",
    "python", "python3", "perl", "ruby", "node", "nodejs", "php", "lua", "tclsh",
    "env", "xargs", "find", "awk", "gawk", "sed", "nc", "ncat", "netcat", "socat", "ssh", "scp",
})


class ExecuteCommand(Tool):
    name = "execute_command"
    description = "Run an allowlisted, read-only diagnostic command inside a sandbox."
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        },
        "required": ["command"],
    }
    risk_level = RiskLevel.R3
    capabilities = frozenset({Capability.EXECUTE})
    idempotent = False
    timeout_s = 15.0

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = args["command"]
        if not isinstance(command, list) or not command or not all(isinstance(c, str) for c in command):
            return ToolResult(ok=False, display="bad command", error="command must be a non-empty argv list")

        perms = ctx.settings.permissions
        # argv[0] must be a BARE name (no '/'): a path lets a planted binary with an allowlisted
        # basename run, and lets the interpreter refusal be bypassed (red-team #7). A bare name is
        # resolved via PATH inside the jail, which only exposes the system bin dirs.
        if "/" in command[0]:
            return ToolResult(ok=False, display="denied",
                              error="command must be a bare binary name, not a path")
        binary = command[0]
        if binary not in set(perms.exec_allowlist):
            return ToolResult(ok=False, display="denied",
                              error=f"'{binary}' is not on the exec allowlist")
        if binary in _INTERPRETERS:
            return ToolResult(ok=False, display="denied",
                              error=f"interpreters/shells are refused: {binary}")

        if perms.jail:
            if not jail.available():
                return ToolResult(ok=False, display="no jail",
                                  error="a sandbox is required but bubblewrap is unavailable")
            # Bind ONLY the exec cwd (not the broad read roots), and mask the deny prefixes,
            # so no secret outside the project is reachable inside the jail (red-team #4).
            argv = jail.build_argv(
                command, read_binds=[perms.exec_cwd], cwd=perms.exec_cwd,
                allow_network=perms.exec_allow_network, deny=perms.fs_deny,
            )
        else:
            argv = command  # explicit, configured opt-out only

        try:
            rc, out, err = await run_argv(argv, timeout=self.timeout_s, limits=True)
        except CommandTimeout as exc:
            return ToolResult(ok=False, display="timeout", error=str(exc))
        except FileNotFoundError:
            return ToolResult(ok=False, display="not found", error=f"{binary} not found")

        return ToolResult(
            ok=(rc == 0),
            output={"returncode": rc, "stdout": out[:_MAX], "stderr": err[:_MAX]},
            display=f"exit {rc}",
            error=None if rc == 0 else (err.strip()[:200] or f"exit {rc}"),
        )


def register_builtins(registry: ToolRegistry) -> None:
    registry.register(ExecuteCommand())
