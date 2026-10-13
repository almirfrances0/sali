"""Git tools. Read-only inspection (R0) plus git_pull (R2: network + working-tree mutation).

The repository path is PathGuard-checked (read scope for inspection, write scope for pull) and
git runs via argv (never a shell) with a scrubbed environment.
"""

from __future__ import annotations

from typing import Any

from sali.core.enums import Capability, RiskLevel
from sali.tools.base import Tool, ToolResult
from sali.tools.context import ToolContext
from sali.tools.exec import CommandTimeout, minimal_env, run_argv
from sali.tools.pathguard import PathViolation
from sali.tools.registry import ToolRegistry

_MAX = 32 * 1024
_REPO_ARG = {"type": "object", "properties": {"repo": {"type": "string"}}, "required": ["repo"]}

# Command-line -c overrides beat repo/.git/config, so a poisoned config can't run code via
# core.sshCommand / core.pager / hooks / ext:: transports (red-team crit #2).
_HARDEN = [
    "-c", "protocol.ext.allow=never",
    "-c", "protocol.file.allow=user",
    "-c", "core.fsmonitor=",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.sshCommand=",
    "-c", "core.pager=cat",
]


def _git_env() -> dict[str, str]:
    env = minimal_env()
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "GIT_ALLOW_PROTOCOL": "file:git:http:https:ssh",
    })
    return env


async def _git(ctx: ToolContext, repo: str, sub: list[str], *, write: bool = False) -> ToolResult:
    try:
        path = ctx.paths.check_write(repo) if write else ctx.paths.check_read(repo)
    except PathViolation as exc:
        return ToolResult(ok=False, display="denied", error=str(exc))
    try:
        # git is a TRUSTED tool (not jailed learning) — no rlimits, and a generous timeout so a real
        # `git pull`/`git log` on a large repo isn't killed by a 64 MB file cap or a 20s CPU limit.
        rc, out, err = await run_argv(
            ["git", *_HARDEN, "-C", str(path), *sub], timeout=90.0, env=_git_env()
        )
    except CommandTimeout as exc:
        return ToolResult(ok=False, display="git timeout", error=str(exc))
    except FileNotFoundError:
        return ToolResult(ok=False, display="no git", error="git not installed")
    if rc != 0:
        return ToolResult(ok=False, display=f"git {sub[0]} failed", error=err.strip()[:400] or f"rc={rc}")
    return ToolResult(ok=True, output={"repo": str(path), "stdout": out[:_MAX]},
                      display=out.strip().splitlines()[0][:80] if out.strip() else f"git {sub[0]} ok")


class GitStatus(Tool):
    name = "git_status"
    description = "Show working-tree status (porcelain) of a git repository."
    parameters = _REPO_ARG
    risk_level = RiskLevel.R0

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return await _git(ctx, args["repo"], ["status", "--porcelain=v1", "-b"])


class GitLog(Tool):
    name = "git_log"
    description = "Recent commits of a git repository (one line each)."
    parameters = {
        "type": "object",
        "properties": {"repo": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["repo"],
    }
    risk_level = RiskLevel.R0

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        limit = min(int(args.get("limit") or 15), 100)
        return await _git(ctx, args["repo"], ["log", "--oneline", "-n", str(limit)])


class GitDiff(Tool):
    name = "git_diff"
    description = "Show the diff of a git repository (staged with staged=true)."
    parameters = {
        "type": "object",
        "properties": {"repo": {"type": "string"}, "staged": {"type": "boolean"}},
        "required": ["repo"],
    }
    risk_level = RiskLevel.R0

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        sub = ["diff", "--stat"] + (["--staged"] if args.get("staged") else [])
        return await _git(ctx, args["repo"], sub)


class GitBranch(Tool):
    name = "git_branch"
    description = "List branches of a git repository."
    parameters = _REPO_ARG
    risk_level = RiskLevel.R0

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return await _git(ctx, args["repo"], ["branch", "-vv"])


class GitPull(Tool):
    name = "git_pull"
    description = "Pull the current branch of a git repository (network + working-tree change)."
    parameters = _REPO_ARG
    # R3 (typed-phrase confirm): git fetch executes repo-controlled config on the host, so it
    # warrants the strong gate, not a bare y/N (red-team crit #2).
    risk_level = RiskLevel.R3
    capabilities = frozenset({Capability.NETWORK, Capability.WRITE})
    idempotent = False
    timeout_s = 120.0  # a pull can fetch a lot; exceed the runner's 90s internal git timeout

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return await _git(ctx, args["repo"], ["pull", "--ff-only"], write=True)


def register_builtins(registry: ToolRegistry) -> None:
    for cls in (GitStatus, GitLog, GitDiff, GitBranch, GitPull):
        registry.register(cls())
