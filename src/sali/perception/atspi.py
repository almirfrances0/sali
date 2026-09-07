"""AT-SPI backend: the focused window's accessibility tree, via the system python.

Sali's venv (3.14) has no PyGObject, so this shells the standalone `_atspi_probe.py` out to a python
that does (the desktop's system interpreter) and parses its JSON — degrading cleanly to ``None`` when
the binding, the a11y bus, or a focused app isn't there. `redact_obj` runs over the parsed tree as
defense-in-depth, even though the probe already emits roles + labels only (never field contents).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from sali.perception.base import UiNode
from sali.security.redact import redact_obj

_PROBE = str(Path(__file__).with_name("_atspi_probe.py"))


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":0")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus")
    return env


def _run_probe(system_python: str, max_depth: int, max_nodes: int,
               pid: int = 0) -> dict[str, Any] | None:
    try:
        argv = [system_python, _PROBE, "--max-depth", str(max_depth), "--max-nodes", str(max_nodes)]
        if pid:
            # Which application is focused is a question X11 already answered exactly. Handing the
            # PID over turns the probe's guess into a lookup.
            argv += ["--pid", str(pid)]
        out = subprocess.run(  # noqa: S603 - fixed argv (our own probe), Sali's own machine
            argv, env=_env(), timeout=8, check=True, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        parsed = json.loads(out.stdout or "{}")
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _to_node(raw: Any) -> UiNode | None:
    if not isinstance(raw, dict):
        return None
    raw_kids = raw.get("children", [])
    if not isinstance(raw_kids, list):  # a malformed probe tree must never raise (ui_tree "never raises")
        raw_kids = []
    kids = [n for n in (_to_node(c) for c in raw_kids) if n is not None]
    # `raw` has already been through redact_obj in ui_tree, so any text here is scrubbed.
    return UiNode(role=str(raw.get("role", "?")), name=str(raw.get("name", "")),
                  text=str(raw.get("text", "")), children=kids)


async def ui_tree(system_python: str, *, max_depth: int, max_nodes: int,
                  pid: int = 0) -> tuple[UiNode | None, str]:
    """The focused app's accessibility tree, plus a detail string explaining any unavailability.
    Never raises. `pid` is the focused window's process from X11 — when known it selects the
    application exactly, instead of the probe inferring it from ACTIVE states."""
    parsed = await asyncio.to_thread(_run_probe, system_python, max_depth, max_nodes, pid)
    if parsed is None:
        return None, "accessibility probe didn't run (no system PyGObject / a11y bus)"
    if not parsed.get("ok"):
        return None, str(parsed.get("detail") or "accessibility unavailable")
    safe = redact_obj(parsed.get("tree"))  # defense-in-depth over roles/labels
    node = _to_node(safe)
    if node is None:
        return None, "accessibility returned an empty tree"
    return node, "truncated" if parsed.get("truncated") else "ok"
