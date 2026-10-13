"""Reusable verification strategies for layers ABOVE the tools layer (runtime / crash-recovery).

Effectful tools verify their own effects by overriding ``Tool.verify()`` with the probe primitives in
``sali.tools.probe`` (the tools layer can't import upward into ``verify/``). This module re-exports
those same probes as a composable catalog for code above the tools layer — most importantly
crash-recovery re-checking, independent of any live tool instance, whether a non-idempotent effect
that was in flight when the process died actually landed (spec §8/§9).
"""

from __future__ import annotations

from typing import Protocol

from sali.tools.base import ToolResult, VerifyResult
from sali.tools.probe import (
    binary_available,
    exit_ok,
    package_installed,
    path_absent,
    path_exists,
    pip_installed,
    port_listening,
    service_active,
    service_inactive,
    verify_command,
)


class VerifyStrategy(Protocol):
    async def verify(self, result: ToolResult) -> VerifyResult: ...


def output_present(result: ToolResult) -> VerifyResult:
    """Read-only tools: confirm the tool returned something."""
    if result.ok and result.output:
        return VerifyResult(True, "output present")
    return VerifyResult(False, result.error or "no output produced")


def command_ok(returncode: int, *, detail: str = "") -> VerifyResult:
    """Verify by exit code alone — the honest fallback when reality can't be independently probed."""
    return exit_ok(returncode, detail=detail)


async def verify_effect(command: str) -> VerifyResult | None:
    """Independently RE-OBSERVE a shell command's intended effect (install → package present, service
    control → status, bound port → listener, mkdir/rm → path). Returns None when the effect can't be
    checked. Crash-recovery uses this to decide completed-vs-not for an interrupted non-idempotent run."""
    return await verify_command(command)


__all__ = [
    "VerifyStrategy",
    "output_present",
    "command_ok",
    "verify_effect",
    "binary_available",
    "package_installed",
    "pip_installed",
    "port_listening",
    "service_active",
    "service_inactive",
    "path_exists",
    "path_absent",
]
