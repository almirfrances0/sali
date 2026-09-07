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
    # Whether Almir is AT the machine — the thing that separates "he's ignoring me" from "he isn't
    # there", and the difference between speaking into a pause and talking over his typing.
    presence: str | None = None
    active_task: str | None = None
    recent_files: list[str] = field(default_factory=list)
    recent_commands: list[RanCommand] = field(default_factory=list)
    recent_errors: list[str] = field(default_factory=list)
    # What Sali has LEARNED he cannot do here (binaries that are absent or need privilege). Durable,
    # unlike everything else on this snapshot — it is the one part of "the machine right now" that
    # was earned from past attempts rather than probed this turn.
    constraints: list[dict[str, Any]] = field(default_factory=list)
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
        return not (self.presence or self.focused_app or self.active_task or self.recent_files
                    or self.recent_commands or self.constraints
                    or self.recent_errors or self.gpu or self.memory or self.disk)

    def render(self) -> str:
        """A compact note for the prompt — only lines that have content (no bloat)."""
        lines: list[str] = []
        if self.focused_app:
            focus = self.focused_app + (f" — {self.focused_window}" if self.focused_window else "")
            lines.append(f"- Focused: {focus}")
        if self.presence:
            lines.append(f"- {self.presence}")
        if self.active_task:
            lines.append(f"- Working on: {self.active_task}")
        if self.recent_files:
            # NOT "Almir changed these". The watcher sees the filesystem, not the hand on it: an ssh
            # session, a build, a background job and Almir all look identical from here. Labelling it
            # neutrally is the difference between reporting an observation and inventing a person's
            # afternoon — Sali told Almir "you were modifying cleanup.py" about edits Almir never made,
            # and had to be corrected twice.
            lines.append("- Changed on disk recently (by whom is unknown — could be Almir, you, or "
                         "another process; do not assume): " + "; ".join(self.recent_files[:4]))
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
        head = "What's happening on the machine right now:\n" + "\n".join(lines) if lines else ""
        # Learned limits are appended as their own titled block rather than mixed into the live
        # lines above: "ping is not installed here" is not something happening now, it is something
        # Sali knows. Keeping them distinct is what stops a durable fact reading as a fresh event.
        from sali.learning.environment import render_constraints
        learned = render_constraints(self.constraints)
        if learned:
            return head + "\n\n" + learned if head else learned
        return head


class WorldStateBuilder:
    """Assembles a WorldState. `perception` is an optional sink exposing snapshot()."""

    def __init__(self, pool: Any, perception: Any = None) -> None:
        self._pool = pool
        self._perception = perception

    async def snapshot(self, *, with_resources: bool = False,
                       with_files: bool = True) -> WorldState:
        ws = WorldState()
        await self._add_focus(ws)
        if with_resources:
            await self._add_resources(ws)
        async with self._pool.acquire() as conn:
            ws.active_task = await conn.fetchval(
                "SELECT objective FROM task WHERE status IN ('open','running') ORDER BY updated_at DESC LIMIT 1")
            # "Recently changed" means FILE/window activity — NOT system events. Excluding port/service/
            # disk observations here stops a "new listening socket …" note being mislabeled as a recent
            # change and injected P1 every turn, which is what once seeded a stray `ss` referent (§14).
            # OFF for ordinary conversation. Relabelling this list as author-unknown was not enough:
            # asked "hi", Sali still answered "I saw YOU poking at test_chat_honesty.py … since you
            # said you were just watching" — inventing both the actor and a quote. A list of files
            # someone touched is genuinely useful when the turn is ABOUT the machine, and is pure
            # confabulation fuel when it is not, so it is now supplied only when it was asked for.
            ws.recent_files = [] if not with_files else [
                str((r["payload"] or {}).get("summary", "")) for r in await conn.fetch(
                    "SELECT payload FROM event WHERE event_type='desktop.observed' "
                    "AND coalesce(payload->>'kind','') NOT IN ('port_opened','service_failed','disk_pressure') "
                    "ORDER BY created_at DESC LIMIT 5")
                if (r["payload"] or {}).get("summary")]
            cmd_rows = await conn.fetch(
                "SELECT plan->'args'->>'command' AS command, success, error FROM tool_execution "
                "WHERE tool_name='execute_command' AND plan->'args'->>'command' IS NOT NULL "
                "ORDER BY started_at DESC LIMIT 6")
            # Best-effort: an empty list (a fresh install, a failed read) simply renders nothing.
            with contextlib.suppress(Exception):
                from sali.learning.environment import learned_constraints
                ws.constraints = await learned_constraints(conn)
        for r in cmd_rows:
            binary = binary_of(str(r["command"]))
            if binary:
                ws.recent_commands.append(RanCommand(binary=binary, ok=r["success"]))
            if r["success"] is False and r["error"]:
                ws.recent_errors.append(str(r["error"]))
        return ws

    async def _add_focus(self, ws: WorldState) -> None:
        # Presence does not depend on the perception backend — it is read straight from the X server,
        # so Sali still knows whether Almir is there even when the window probe is unavailable.
        with contextlib.suppress(Exception):
            from sali.perception import presence

            ws.presence = presence.describe()
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
