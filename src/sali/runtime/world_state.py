"""Sali's live World-State — "what is happening now?" (spec §5/§18/§72/§73).

So a turn ALREADY knows the environment before Almir even finishes asking (§73): the focused app/
window, the files that just changed, the commands just run and how they fared, and the task in flight.
Assembled on demand from signals that already exist — a best-effort perception probe for the focused
window, and the DB-shared channels the background perception engine + execution broker already write
(`desktop.observed` events, `tool_execution` rows). It composes existing state; it stores nothing new.
Read-only: the world-state never acts (§46).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any

from sali.core.toolvocab import binary_of


@dataclass(slots=True)
class RanCommand:
    binary: str
    ok: bool | None


@dataclass(slots=True)
class WorldState:
    focused_app: str | None = None
    focused_window: str | None = None
    active_task: str | None = None
    recent_files: list[str] = field(default_factory=list)     # summaries of recent fs observations
    recent_commands: list[RanCommand] = field(default_factory=list)
    recent_errors: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.focused_app or self.active_task or self.recent_files
                    or self.recent_commands or self.recent_errors)

    def render(self) -> str:
        """A compact note for the prompt — only the lines that have content (no bloat)."""
        lines: list[str] = []
        if self.focused_app:
            focus = self.focused_app + (f" — {self.focused_window}" if self.focused_window else "")
            lines.append(f"- Focused: {focus}")
        if self.active_task:
            lines.append(f"- Working on: {self.active_task}")
        if self.recent_files:
            lines.append("- Recently changed: " + "; ".join(self.recent_files[:4]))
        if self.recent_commands:
            cmds = ", ".join(
                f"{c.binary}{'' if c.ok is None else ' (ok)' if c.ok else ' (failed)'}"
                for c in self.recent_commands[:5])
            lines.append(f"- Recent commands: {cmds}")
        if self.recent_errors:
            lines.append("- Recent errors: " + "; ".join(e[:120] for e in self.recent_errors[:3]))
        return "What's happening on the machine right now:\n" + "\n".join(lines) if lines else ""


class WorldStateBuilder:
    """Assembles a WorldState. `perception` is an optional sink exposing snapshot()."""

    def __init__(self, pool: Any, perception: Any = None) -> None:
        self._pool = pool
        self._perception = perception

    async def snapshot(self) -> WorldState:
        ws = WorldState()
        await self._add_focus(ws)
        async with self._pool.acquire() as conn:
            ws.active_task = await conn.fetchval(
                "SELECT objective FROM task WHERE status IN ('open','running') "
                "ORDER BY updated_at DESC LIMIT 1")
            ws.recent_files = [
                str((r["payload"] or {}).get("summary", "")) for r in await conn.fetch(
                    "SELECT payload FROM event WHERE event_type='desktop.observed' "
                    "ORDER BY created_at DESC LIMIT 5")
                if (r["payload"] or {}).get("summary")]
            cmd_rows = await conn.fetch(
                "SELECT plan->'args'->>'command' AS command, success, error FROM tool_execution "
                "WHERE tool_name='execute_command' AND plan->'args'->>'command' IS NOT NULL "
                "ORDER BY started_at DESC LIMIT 6")
        for r in cmd_rows:
            binary = binary_of(str(r["command"]))
            if binary:
                ws.recent_commands.append(RanCommand(binary=binary, ok=r["success"]))
            if r["success"] is False and r["error"]:
                ws.recent_errors.append(str(r["error"]))
        return ws

    async def _add_focus(self, ws: WorldState) -> None:
        if self._perception is None:
            return
        with contextlib.suppress(Exception):  # a perception hiccup must never break a turn
            snap = await self._perception.snapshot(ui=False)
            window = (snap or {}).get("window") or {}
            ws.focused_app = (window.get("app") or "").strip() or None
            ws.focused_window = (window.get("title") or "").strip() or None
