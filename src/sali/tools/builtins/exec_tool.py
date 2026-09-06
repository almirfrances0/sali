"""execute_command — Sali runs commands on its own machine, freely.

This is Sali's home, so ordinary commands (checking things, installing tools, poking around)
run without asking. The tool only *escalates itself* to a confirmation when the command is
genuinely destructive (rm -rf, mkfs, dd to a device, a fork bomb, …) — the policy then pauses
so Sali (and Almir) think first, the way anyone would before wrecking their own system.

For risky experiments there's ``sandbox=true``: the command runs inside a throwaway bubblewrap
jail so installing/deleting can't touch the real system (used during learning).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from sali.core.enums import Capability, RiskLevel
from sali.tools import jail, privilege
from sali.tools.base import Tool, ToolResult, VerifyResult
from sali.tools.context import ToolContext
from sali.tools.exec import CommandTimeout, run_argv
from sali.tools.probe import exit_ok, verify_command
from sali.tools.registry import ToolRegistry

_MAX = 256 * 1024

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
        "need. For a LONG-RUNNING process (a dev/web server like `python3 -m http.server`, a "
        "watcher) pass background=true so it starts and keeps running WITHOUT blocking you — its "
        "output goes to a log file you can read later. (A foreground server would just time out.) "
        "Pass sandbox=true to run in a throwaway isolated environment for risky tests."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}],
                "description": "A shell command string, or an argv list.",
            },
            "background": {"type": "boolean",
                           "description": "Start detached and return at once (servers, watchers)."},
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

    async def verify(
        self, args: dict[str, Any], result: ToolResult, ctx: ToolContext
    ) -> VerifyResult:
        """Independently re-observe the command's effect (§8/§23): after an install, confirm the
        package is present; after `systemctl start`, that the service is active; after binding a port,
        that something is listening — never trusting the exit code alone. Falls back to the exit code
        when no probe applies (a plain read) or the effect can't be observed, and never blocks."""
        output = result.output or {}
        if args.get("sandbox") or output.get("background"):
            # sandbox effects never touch the real system; a backgrounded server's port isn't knowable
            # from the command text alone — trust the launch/exit signal run() already checked.
            state = "sandboxed" if args.get("sandbox") else "launched"
            return VerifyResult(result.ok, result.display or state)
        probe = await verify_command(_as_text(args.get("command")))
        if probe is not None:
            return probe  # verified against reality
        rc = int(output.get("returncode", 0 if result.ok else 1))
        return exit_ok(rc)

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = args.get("command")
        if isinstance(command, str):
            # §28: if the command uses sudo, route it through the askpass broker so the password comes
            # from the vault, never the model. No-op when there's no sudo / no password configured.
            argv = ["bash", "-c", privilege.wrap_sudo(command)]
        elif isinstance(command, list) and command and all(isinstance(c, str) for c in command):
            argv = command
        else:
            return ToolResult(ok=False, display="bad command", error="command must be a string or argv list")

        perms = ctx.settings.permissions
        sandbox = bool(args.get("sandbox"))

        # Use workspace root as cwd when a task workspace is active.
        cwd = str(ctx.workspace.workspace_root) if ctx.workspace else str(Path(perms.exec_cwd).expanduser())

        if bool(args.get("background")):
            # A long-running process (server, watcher): launch it detached in its own session so it
            # keeps running after this call returns, with output tee'd to a log Sali can read. This
            # is what stops `python3 -m http.server` from blocking and timing out forever.
            return await _run_background(argv, cwd=cwd)

        if sandbox:
            if not (perms.jail_learning and jail.available()):
                return ToolResult(ok=False, display="no sandbox",
                                  error="the learning sandbox needs bubblewrap")
            # Fake-root, writable-ephemeral system: install/delete freely, host untouched.
            argv = jail.build_sandbox_argv(argv, allow_network=perms.exec_allow_network)

        # §7/§8/§53: consequential (non-sandbox) commands are recorded in the SideEffectStore ledger
        # so a partially-completed workflow stays VISIBLE and recoverable after interruption or
        # restart. Idempotency check via idempotency_key (command text + cwd) prevents accidental
        # re-execution of an already-succeeded destructive command. The plan/attempt/succeed|fail
        # lifecycle was previously dormant — nothing outside the store's own file called it.
        side_effect_id = None
        skip_run = False
        if not sandbox and _is_destructive(command):
            side_effect_id = await self._plan_side_effect(ctx, command, cwd)
            if side_effect_id == "ALREADY_DONE":
                # An identical destructive command succeeded before — §53 idempotency: return the
                # prior success instead of executing again.
                return ToolResult(ok=True, display="already done (idempotent)",
                                  output={"returncode": 0, "stdout": "", "stderr": "",
                                          "idempotent": True})
            if side_effect_id is not None:
                with contextlib.suppress(Exception):
                    from sali.tasks.side_effects import SideEffectStore

                    pool = getattr(ctx, "pool", None) or getattr(ctx.settings, "pool", None)
                    if pool is not None:
                        await SideEffectStore(pool).attempt(side_effect_id)

        try:
            # 1 MiB before the flood-kill (was 256 KiB) — a real log/build/journalctl dump shouldn't
            # be SIGKILLed mid-run; the loop still truncates what the model sees to _TOOL_OUTPUT_CAP.
            rc, out, err = await run_argv(
                argv, timeout=self.timeout_s, env=privilege.sudo_env(_safe_env()),
                max_output=1024 * 1024, cwd=cwd,
            )
        except CommandTimeout as exc:
            if side_effect_id is not None:
                await self._settle_side_effect(ctx, side_effect_id, ok=False, error=str(exc))
            return ToolResult(ok=False, display="timeout", error=str(exc))
        except FileNotFoundError as exc:
            if side_effect_id is not None:
                await self._settle_side_effect(ctx, side_effect_id, ok=False, error=str(exc))
            return ToolResult(ok=False, display="not found", error=str(exc))

        if side_effect_id is not None:
            await self._settle_side_effect(
                ctx, side_effect_id, ok=(rc == 0),
                error=None if rc == 0 else (err.strip()[:300] or f"exit {rc}"))
        return ToolResult(
            ok=(rc == 0),
            output={"returncode": rc, "stdout": out[:_MAX], "stderr": err[:_MAX], "sandboxed": sandbox},
            display=f"exit {rc}" + (" (sandboxed)" if sandbox else ""),
            error=None if rc == 0 else (err.strip()[:300] or f"exit {rc}"),
        )

    async def _plan_side_effect(self, ctx: ToolContext, command: Any, cwd: str) -> Any:
        """Register the intended destructive action in SideEffectStore. Returns the id, or the
        string 'ALREADY_DONE' if idempotency check found a prior success, or None on any error."""
        try:
            from sali.tasks.side_effects import SideEffectStore

            pool = getattr(ctx, "pool", None) or getattr(ctx.settings, "pool", None)
            if pool is None:
                return None
            key = f"exec::{cwd}::{_as_text(command)[:400]}"
            store = SideEffectStore(pool)
            done = await store.already_done(key)
            if done is not None:
                return "ALREADY_DONE"
            sid, was_done = await store.plan(
                kind="command_exec", target=_as_text(command)[:200],
                task_id=getattr(ctx, "task_id", None), run_id=getattr(ctx, "run_id", None),
                idempotency_key=key)
            return "ALREADY_DONE" if was_done else sid
        except Exception:  # noqa: BLE001 - ledger is a safety net, never blocks the real command
            return None

    async def _settle_side_effect(self, ctx: ToolContext, side_effect_id: Any, *,
                                    ok: bool, error: str | None = None) -> None:
        with contextlib.suppress(Exception):
            from sali.tasks.side_effects import SideEffectStore

            pool = getattr(ctx, "pool", None) or getattr(ctx.settings, "pool", None)
            if pool is None:
                return
            store = SideEffectStore(pool)
            if ok:
                await store.succeed(side_effect_id, after_state="completed")
            else:
                await store.fail(side_effect_id, error=error)


async def _run_background(argv: list[str], *, cwd: str) -> ToolResult:
    """Start a detached process (own session, output → a log file) and return once we've confirmed it
    survived launch — a command that dies instantly (bad binary → 127, bad args) is reported as the
    failure it is, never as a happily-running background job (verify the effect, §23)."""
    logdir = Path.home() / ".local" / "share" / "sali" / "bg"
    try:
        logdir.mkdir(parents=True, exist_ok=True)
        fd, log_path = tempfile.mkstemp(prefix="bg-", suffix=".log", dir=str(logdir))
        with os.fdopen(fd, "wb") as log:
            proc = subprocess.Popen(  # noqa: S603 - Sali runs freely on its own machine
                argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                start_new_session=True, env=privilege.sudo_env(_safe_env()), cwd=cwd,
            )
    except (OSError, ValueError) as exc:
        return ToolResult(ok=False, display="couldn't start", error=str(exc)[:200])
    await asyncio.sleep(0.2)  # give it a beat to fall over, if it's going to
    rc = proc.poll()
    if rc is not None and rc != 0:  # already dead with a failure → surface it, don't claim success
        tail = _log_tail(log_path)
        return ToolResult(ok=False, display=f"exited immediately (rc {rc})",
                          output={"returncode": rc, "log": log_path},
                          error=tail or f"process exited {rc} right after launch")
    return ToolResult(
        ok=True,
        output={"pid": proc.pid, "log": log_path, "background": True},
        display=f"started in background (pid {proc.pid}); output → {log_path}",
    )


def _log_tail(path: str, limit: int = 400) -> str:
    with contextlib.suppress(OSError):
        return Path(path).read_text("utf-8", "replace")[-limit:].strip()
    return ""


def register_builtins(registry: ToolRegistry) -> None:
    registry.register(ExecuteCommand())
