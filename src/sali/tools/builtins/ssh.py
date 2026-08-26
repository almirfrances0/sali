"""SSH / VPS tools — Sali manages remote hosts over Almir's existing ssh (§44).

Reading and ordinary remote work runs FREE (Sali's freedom holds on the machine AND the boxes Almir
already administers). The trust boundary is destruction on a remote box: a command that would destroy
remote data/service self-escalates to R4 → one confirm (the same single-nod path local `rm -rf`
uses), so an unattended run won't wipe a server. Host is a ~/.ssh/config alias or user@host.
"""

from __future__ import annotations

import re
from typing import Any

from sali.core.enums import Capability, RiskLevel
from sali.tools.base import Tool, ToolResult
from sali.tools.context import ToolContext
from sali.tools.registry import ToolRegistry

# Destroys remote data / hardware / a running service → confirm before it leaves for the box.
_DESTRUCTIVE_REMOTE = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\brm\b(?=.*\s-\w*[rf])", r"\brm\b\s+(-\w+\s+)*(/|~|\*)",
        r"\b(mkfs|mke2fs|fdisk|parted|sgdisk|wipefs|shred|blkdiscard|mkswap)\b",
        r"\bdd\b.*\bof=/dev/", r">\s*/dev/(sd|nvme|hd|vd)",
        r"\b(shutdown|reboot|halt|poweroff)\b",
        r"\bsystemctl\b\s+(stop|disable|mask)\b",  # taking a service down
        r"\b(dropdb|drop\s+database|drop\s+table)\b",
        r"\biptables\b\s+-[FX]", r"\bufw\b\s+(disable|reset)\b",
        r"\buserdel\b|\bdeluser\b", r":\(\)\s*\{\s*:\s*\|\s*:",  # fork bomb
    )
]


def _is_destructive_remote(command: Any) -> bool:
    text = command if isinstance(command, str) else ""
    return any(p.search(text) for p in _DESTRUCTIVE_REMOTE)


class SshRun(Tool):
    name = "ssh_run"
    description = (
        "Run a command on a remote host over ssh and get its output. `host` is a ~/.ssh/config alias "
        "or user@host. Ordinary checks (df, systemctl status, tail logs, git pull) run freely; a "
        "genuinely destructive remote command pauses to confirm first. If the host needs a password "
        "(no key set up) pass `password` ONCE — it's stored in the encrypted vault and reused, so you "
        "only supply it the first time. Never pass a password inside `command`."
    )
    parameters = {
        "type": "object",
        "properties": {
            "host": {"type": "string", "description": "~/.ssh/config alias or user@host."},
            "command": {"type": "string", "description": "The command to run on the remote host."},
            "password": {"type": "string",
                         "description": "SSH password, only if the host has no key. Stored encrypted, "
                                        "reused next time — supply it once. Omit for key-based hosts."},
        },
        "required": ["host", "command"],
    }
    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.EXECUTE, Capability.NETWORK})
    idempotent = False
    timeout_s = 600.0  # outer backstop only; the real cap is the runner's ssh.command_timeout

    def assess(self, args: dict[str, Any]) -> RiskLevel:
        return RiskLevel.R4 if _is_destructive_remote(args.get("command")) else RiskLevel.R1

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.remote is None:
            return ToolResult(ok=False, display="no ssh", error="remote execution isn't available")
        host = str(args.get("host", "")).strip()
        command = str(args.get("command", "")).strip()
        password = args.get("password") or None
        if not host or not command:
            return ToolResult(ok=False, display="need host + command",
                              error="host and command are required")
        res = await ctx.remote.run(host, command, password=password)
        return ToolResult(
            ok=res.ok,
            output={"host": res.host, "returncode": res.returncode,
                    "stdout": res.stdout[:_MAX], "stderr": res.stderr[:_MAX]},
            display=f"{host}: exit {res.returncode}",
            error=None if res.ok else (res.stderr.strip()[:300] or "ssh failed"),
        )


class SshPut(Tool):
    name = "ssh_put"
    description = ("Copy a local file to a remote host over scp. Give local path, host, remote path. "
                   "For a password host, pass `password` once (stored encrypted, reused after).")
    parameters = {
        "type": "object",
        "properties": {
            "local": {"type": "string", "description": "The local file to copy."},
            "host": {"type": "string", "description": "~/.ssh/config alias or user@host."},
            "remote": {"type": "string", "description": "Destination path on the remote host."},
            "password": {"type": "string", "description": "SSH password if the host has no key "
                         "(stored encrypted, reused). Omit for key-based hosts."},
        },
        "required": ["local", "host", "remote"],
    }
    risk_level = RiskLevel.R2  # a remote write; auto-allowed under the freedom policy
    capabilities = frozenset({Capability.EXECUTE, Capability.NETWORK, Capability.WRITE})
    idempotent = False
    timeout_s = 600.0  # outer backstop; scp of a large file can take a while

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.remote is None:
            return ToolResult(ok=False, display="no ssh", error="remote execution isn't available")
        local, host, remote = (str(args.get(k, "")).strip() for k in ("local", "host", "remote"))
        password = args.get("password") or None
        if not (local and host and remote):
            return ToolResult(ok=False, display="need local, host, remote",
                              error="local, host and remote are all required")
        res = await ctx.remote.put(host, local, remote, password=password)
        # For scp the exit code IS the verdict (unlike ssh_run, where a non-zero remote rc is data):
        # only rc 0 means the file actually landed. Never report a failed copy as success.
        ok = res.returncode == 0
        return ToolResult(
            ok=ok,
            output={"host": host, "returncode": res.returncode, "remote": remote},
            display=f"copied to {host}:{remote}" if ok else f"{host}: scp failed (exit {res.returncode})",
            error=None if ok else (res.stderr.strip()[:300] or f"scp exit {res.returncode}"),
        )


_MAX = 64 * 1024


def register_builtins(registry: ToolRegistry) -> None:
    for tool in (SshRun(), SshPut()):
        registry.register(tool)
