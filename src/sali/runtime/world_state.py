"""Sali's live World-State — "what is happening now?" (spec §5/§18/§72/§73).

So a turn ALREADY knows the environment before Almir even finishes asking (§73): the focused app/
window, the files that just changed, the commands just run and how they fared, the task in flight, and
(when asked) the live host resources. Assembled on demand from signals that already exist — a best-effort
perception probe, the DB-shared channels the perception engine + broker write, and the same deterministic
readers the system_* tools use. It composes existing state; it stores nothing new. Read-only (§46).
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
    recent_files: list[str] = field(default_factory=list)
    recent_commands: list[RanCommand] = field(default_factory=list)
    recent_errors: list[str] = field(default_factory=list)
    # live host resources — filled only when a turn/query needs them (probing every trivial turn is waste)
    gpu: dict[str, Any] | None = None
    memory: dict[str, Any] | None = None
    disk: dict[str, Any] | None = None
    cpu_pct: float | None = None
    uptime_s: int | None = None
    processes: int | None = None
    listen_ports: int | None = None
    services: int | None = None
    containers: int | None = None

    def is_empty(self) -> bool:
        return not (self.focused_app or self.active_task or self.recent_files or self.recent_commands
                    or self.recent_errors or self.gpu or self.memory or self.disk)

    def render(self) -> str:
        """A compact note for the prompt — only lines that have content (no bloat)."""
        lines: list[str] = []
        if self.focused_app:
            focus = self.focused_app + (f" — {self.focused_window}" if self.focused_window else "")
            lines.append(f"- Focused: {focus}")
        if self.active_task:
            lines.append(f"- Working on: {self.active_task}")
        if self.recent_files:
            lines.append("- Recently changed: " + "; ".join(self.recent_files[:4]))
        if self.recent_commands:
            cmds = ", ".join(f"{c.binary}{'' if c.ok is None else ' (ok)' if c.ok else ' (failed)'}"
                             for c in self.recent_commands[:5])
            lines.append(f"- Recent commands: {cmds}")
        if self.memory:
            lines.append(f"- Memory: {self.memory['used_mib']}/{self.memory['total_mib']} MiB used")
        if self.cpu_pct is not None:
            lines.append(f"- CPU: {self.cpu_pct}%")
        if self.disk:
            lines.append(f"- Disk: {self.disk['used_gib']}/{self.disk['total_gib']} GiB ({self.disk['percent_used']}%)")
        if self.gpu:
            t = f", {self.gpu['temperature_c']}C" if self.gpu.get('temperature_c') is not None else ""
            lines.append(f"- GPU: {self.gpu['name']} — {self.gpu['vram_used_mib']}/{self.gpu['vram_total_mib']} MiB VRAM, {self.gpu['gpu_util_percent']}% util{t}")
        if self.recent_errors:
            lines.append("- Recent errors: " + "; ".join(e[:120] for e in self.recent_errors[:3]))
        return "What's happening on the machine right now:\n" + "\n".join(lines) if lines else ""


class WorldStateBuilder:
    """Assembles a WorldState. `perception` is an optional sink exposing snapshot()."""

    def __init__(self, pool: Any, perception: Any = None) -> None:
        self._pool = pool
        self._perception = perception

    async def snapshot(self, *, with_resources: bool = False) -> WorldState:
        ws = WorldState()
        await self._add_focus(ws)
        if with_resources:
            await self._add_resources(ws)
        async with self._pool.acquire() as conn:
            ws.active_task = await conn.fetchval(
                "SELECT objective FROM task WHERE status IN ('open','running') ORDER BY updated_at DESC LIMIT 1")
            ws.recent_files = [
                str((r["payload"] or {}).get("summary", "")) for r in await conn.fetch(
                    "SELECT payload FROM event WHERE event_type='desktop.observed' ORDER BY created_at DESC LIMIT 5")
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
        with contextlib.suppress(Exception):
            snap = await self._perception.snapshot(ui=False)
            window = (snap or {}).get("window") or {}
            ws.focused_app = (window.get("app") or "").strip() or None
            ws.focused_window = (window.get("title") or "").strip() or None

    async def _add_resources(self, ws: WorldState) -> None:
        """Live host resources, from the SAME deterministic readers the system_* tools use (§7). Each is
        best-effort — a missing GPU or a slow probe must never break the turn."""
        from sali.tools.builtins.system import (
            read_cpu,
            read_disk,
            read_gpu,
            read_listen_ports,
            read_memory,
            read_processes,
            read_uptime,
        )
        with contextlib.suppress(Exception):
            ws.memory = read_memory()
        with contextlib.suppress(Exception):
            ws.disk = read_disk("/")
        with contextlib.suppress(Exception):
            ws.gpu = await read_gpu()
        with contextlib.suppress(Exception):
            ws.cpu_pct = await read_cpu()
        with contextlib.suppress(Exception):
            ws.uptime_s = read_uptime()
        with contextlib.suppress(Exception):
            ws.processes = read_processes()
        with contextlib.suppress(Exception):
            ws.listen_ports = read_listen_ports()
        with contextlib.suppress(Exception):
            async with self._pool.acquire() as conn:
                ws.services = await conn.fetchval(
                    "SELECT count(*) FROM graph_node WHERE node_type='service' AND valid_until IS NULL")
                ws.containers = await conn.fetchval(
                    "SELECT count(*) FROM graph_node WHERE node_type='container' AND valid_until IS NULL")
