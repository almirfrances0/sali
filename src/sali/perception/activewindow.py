"""Active-window perception over X11 (xdotool + xprop) — instant, always-on, no a11y bus needed.

This is the cheapest desktop signal and the one Sali needs most often: which application is focused
and what its window says. Runs the small X utilities in a worker thread so the loop never blocks.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess

from sali.perception.base import WindowInfo
from sali.security.redact import redact


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":0")  # a background daemon has no DISPLAY; use the logged-in session
    return env


def _run(argv: list[str]) -> str | None:
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, Sali's own machine
            argv, env=_env(), timeout=5, check=True, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout


def _wm_class(xprop_out: str) -> str | None:
    # WM_CLASS(STRING) = "instance", "Class"  → prefer the (more specific) instance name. Require the
    # '=' (like _pid): a window with no WM_CLASS makes xprop print "WM_CLASS:  not found." (exit 0),
    # which must fall through to the "unknown" default, not become the app name.
    for line in xprop_out.splitlines():
        if line.startswith("WM_CLASS") and "=" in line:
            parts = [p.strip().strip('"') for p in line.split("=", 1)[-1].split(",")]
            parts = [p for p in parts if p]
            if parts:
                return parts[0]
    return None


def _pid(xprop_out: str) -> int | None:
    for line in xprop_out.splitlines():
        if "_NET_WM_PID" in line and "=" in line:
            try:
                return int(line.split("=", 1)[-1].strip())
            except ValueError:
                return None
    return None


def _geometry(shell_out: str) -> tuple[int, int, int, int] | None:
    vals: dict[str, int] = {}
    for line in shell_out.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            try:
                vals[k.strip()] = int(v.strip())
            except ValueError:
                continue
    if {"X", "Y", "WIDTH", "HEIGHT"} <= vals.keys():
        return (vals["X"], vals["Y"], vals["WIDTH"], vals["HEIGHT"])
    return None


def _read_sync() -> WindowInfo | None:
    if shutil.which("xdotool") is None:
        return None
    win_id = _run(["xdotool", "getactivewindow"])
    if not win_id or not win_id.strip():
        return None
    wid = win_id.strip().splitlines()[0]
    title = (_run(["xdotool", "getwindowname", wid]) or "").strip()
    xprop = _run(["xprop", "-id", wid, "WM_CLASS", "_NET_WM_PID"]) or "" if shutil.which("xprop") else ""
    app = _wm_class(xprop) or "unknown"
    pid = _pid(xprop)
    geo = _geometry(_run(["xdotool", "getwindowgeometry", "--shell", wid]) or "")
    # A window title can carry a secret (a terminal titled with the running command, a tokened URL).
    # Scrub it before it reaches the model/logs — §27, same defense-in-depth as the UI tree. redact()
    # now covers command-line credential forms (mysql -p…, --password, curl -u u:p) too.
    return WindowInfo(app=app, title=redact(title), pid=pid, geometry=geo)


async def active_window() -> WindowInfo | None:
    """The focused window, or None if there's no display / xdotool. Never raises."""
    return await asyncio.to_thread(_read_sync)
