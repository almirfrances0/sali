"""Confirmation collectors.

The policy *decides*; a confirmer *collects* a human yes/no when the decision is CONFIRM.
The decision stays server-side; a confirmer is just the UI. R4 (were it ever routed here)
would demand a typed exact phrase, never a bare 'y'.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Protocol

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
    """Prompts at the terminal for a simple y/N — only for genuinely destructive acts.

    Confirmation happens mid-turn while a live spinner may own the terminal, so this PAUSES that
    live display first (``attach``-ed by the CLI) and reads the answer OFF the event loop
    (asyncio.to_thread), so the prompt is actually visible and the loop never freezes on a blocking
    read — the deadlock that used to hang a turn forever on the first R4."""

    def __init__(self) -> None:
        from rich.console import Console

        self._console = Console()
        self.live: Any = None  # the CLI attaches its rich.Live here so we can pause it to prompt

    def attach(self, live: Any) -> None:
        self.live = live

    async def confirm(self, tool: Tool, args: dict[str, object], decision: PolicyDecision) -> bool:
        detail = args.get("command") or args.get("path") or args
        live = self.live
        if live is not None:
            with contextlib.suppress(Exception):
                live.stop()  # give the terminal back so the prompt shows and stdin is clean
        self._console.print(
            f"[yellow]⚠ Sali wants to do something destructive[/] — [bold]{tool.name}[/]: {detail}"
        )
        try:
            answer = await asyncio.to_thread(input, "let it? [y/N] ")  # off-loop: never freezes
        except (EOFError, KeyboardInterrupt):
            answer = ""
        finally:
            if live is not None:
                with contextlib.suppress(Exception):
                    live.start(refresh=True)
        return answer.strip().lower() in {"y", "yes", "ok", "yeah", "sure", "go"}
