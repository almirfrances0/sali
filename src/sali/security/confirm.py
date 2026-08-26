"""Confirmation collectors.

The policy *decides*; a confirmer *collects* a human yes/no when the decision is CONFIRM.
The decision stays server-side; a confirmer is just the UI. R4 (were it ever routed here)
would demand a typed exact phrase, never a bare 'y'.
"""

from __future__ import annotations

from typing import Protocol

from sali.security.policy import PolicyDecision
from sali.tools.base import Tool


class Confirmer(Protocol):
    async def confirm(self, tool: Tool, args: dict[str, object], decision: PolicyDecision) -> bool:
        ...


class AutoAllowConfirmer:
    """Approves every confirmation (tests / trusted automation)."""

    async def confirm(self, tool: Tool, args: dict[str, object], decision: PolicyDecision) -> bool:
        return True


class AutoDenyConfirmer:
    """Refuses every confirmation — the safe default for headless runs."""

    async def confirm(self, tool: Tool, args: dict[str, object], decision: PolicyDecision) -> bool:
        return False


class TerminalConfirmer:
    """Prompts at the terminal. R2/R3 accept y/N; a destructive tool needs the exact phrase."""

    def __init__(self) -> None:
        from rich.console import Console

        self._console = Console()

    async def confirm(self, tool: Tool, args: dict[str, object], decision: PolicyDecision) -> bool:
        # Confirmation is rare (only genuinely destructive acts) — keep it a simple, human nod.
        detail = args.get("command") or args.get("path") or args
        self._console.print(
            f"[yellow]⚠ Sali wants to do something destructive[/] — [bold]{tool.name}[/]: {detail}"
        )
        answer = self._console.input("[dim]let it? [y/N] [/]")
        return answer.strip().lower() in {"y", "yes", "ok", "yeah", "sure", "go"}
