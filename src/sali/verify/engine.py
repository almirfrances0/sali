"""Reusable verification strategies.

The pattern is generic: after a tool runs, check a post-condition (output present, exit
zero, a status probe, an HTTP health check) and return a :class:`VerifyResult`. Read-only
tools use :func:`output_present`; effectful tools (restart a service → check status → curl a
health endpoint) will compose these in a later phase.
"""

from __future__ import annotations

from typing import Protocol

from sali.tools.base import ToolResult, VerifyResult


class VerifyStrategy(Protocol):
    async def verify(self, result: ToolResult) -> VerifyResult: ...


def output_present(result: ToolResult) -> VerifyResult:
    if result.ok and result.output:
        return VerifyResult(True, "output present")
    return VerifyResult(False, result.error or "no output produced")


def command_ok(returncode: int, *, detail: str = "") -> VerifyResult:
    if returncode == 0:
        return VerifyResult(True, detail or "exit 0")
    return VerifyResult(False, detail or f"non-zero exit {returncode}")
