"""active_window — Sali's cheapest look at the desktop (sali3 Phase 4, §2,8,9).

The focused application + window title, instant and local, via X11. This is Sali's FIRST way to know
what Almir is doing on screen — far cheaper than vision; reach for `see_screen` only when you need to
see actual pixels. Set ui=true to also pull the focused window's accessibility tree (roles + labels,
never field contents) when the a11y bus is available.
"""

from __future__ import annotations

from typing import Any

from sali.core.enums import Capability, RiskLevel
from sali.tools.base import Tool, ToolResult
from sali.tools.context import ToolContext
from sali.tools.registry import ToolRegistry

_MAX = 12 * 1024


class ActiveWindow(Tool):
    name = "active_window"
    description = (
        "See what Almir is doing right now — the focused application and its window title, instant "
        "and local. This is your cheapest, first-choice way to know the on-screen context; use "
        "see_screen only when you must see the actual pixels. Set ui=true to also read the focused "
        "window's accessibility tree (buttons, labels, fields — structure only, never their "
        "contents) when it's available."
    )
    parameters = {
        "type": "object",
        "properties": {
            "ui": {"type": "boolean",
                   "description": "Also include the focused window's accessibility tree."},
        },
        "required": [],
    }
    risk_level = RiskLevel.R1  # reading Almir's own desktop for him — benign, stays local
    capabilities = frozenset({Capability.EXECUTE})  # spawns xdotool/xprop
    timeout_s = 15.0
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.perception is None:
            return ToolResult(ok=False, display="no perception",
                              error="desktop perception isn't available")
        snap = await ctx.perception.snapshot(ui=bool(args.get("ui")))
        if not snap.get("available"):
            return ToolResult(ok=False, display="can't read the desktop",
                              error=str(snap.get("detail") or "no active window / no display"))
        win = snap.get("window") or {}
        label = f"{win.get('app', '?')} — {win.get('title', '')}".strip(" —") or "the desktop"
        return ToolResult(ok=True, output=_cap(snap), display=f"looking at {label}"[:120])


def _cap(snap: dict[str, Any]) -> dict[str, Any]:
    # Guard against a pathological UI tree blowing up the observation; the window info is tiny.
    import json

    if snap.get("ui") is not None and len(json.dumps(snap["ui"])) > _MAX:
        snap = {**snap, "ui": None, "detail": "ui tree omitted (too large)"}
    return snap


def register_builtins(registry: ToolRegistry) -> None:
    registry.register(ActiveWindow())
