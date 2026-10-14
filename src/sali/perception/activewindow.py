"""Active-window perception — X11 (xdotool + xprop) or Wayland/GNOME (AT-SPI).

X11 path: xdotool + xprop — instant, no a11y bus needed. Used when xdotool is installed.
Wayland path: AT-SPI via the system python3 — returns the focused application's name, its
top-level frame title, and its PID. Works on GNOME Wayland without any X11 tooling.

Sali reads the focused window every 3 s (syswatch) and on-demand when Almir asks "what am I
looking at?". Both paths return the same WindowInfo shape.

Runs in a worker thread via asyncio.to_thread so the event loop never blocks on a subprocess.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess

from sali.perception.base import WindowInfo
from sali.security.redact import redact

# ── X11 helpers (unchanged) ──────────────────────────────────────────────────────────────────────

def _x11_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":0")
    return env


def _run(argv: list[str], env: dict[str, str] | None = None) -> str | None:
    try:
        out = subprocess.run(  # noqa: S603
            argv, env=env or _x11_env(), timeout=5, check=True, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout


def _wm_class(xprop_out: str) -> str | None:
    for line in xprop_out.splitlines():
        if line.startswith("WM_CLASS") and "=" in line:
            parts = [p.strip().strip('"') for p in line.split("=", 1)[-1].split(",")]
            parts = [p for p in parts if p]
            if parts:
                return parts[0]
    return None


def _pid_from_xprop(xprop_out: str) -> int | None:
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


def _read_x11() -> WindowInfo | None:
    """X11 path: xdotool + xprop. Returns None when xdotool is absent or no window is focused."""
    if shutil.which("xdotool") is None:
        return None
    win_id = _run(["xdotool", "getactivewindow"])
    if not win_id or not win_id.strip():
        return None
    wid = win_id.strip().splitlines()[0]
    title = (_run(["xdotool", "getwindowname", wid]) or "").strip()
    xprop = (_run(["xprop", "-id", wid, "WM_CLASS", "_NET_WM_PID"]) or ""
             if shutil.which("xprop") else "")
    app = _wm_class(xprop) or "unknown"
    pid = _pid_from_xprop(xprop)
    geo = _geometry(_run(["xdotool", "getwindowgeometry", "--shell", wid]) or "")
    return WindowInfo(app=app, title=redact(title), pid=pid, geometry=geo)


# ── AT-SPI / Wayland path ─────────────────────────────────────────────────────────────────────────

# System python3 has gi + Atspi; Sali's 3.14 venv does not. The same interpreter used by
# _atspi_probe.py — found from settings.system_python, defaulting to /usr/bin/python3.
_SYSTEM_PYTHON: str = "/usr/bin/python3"   # overridden by set_system_python()

# Applications that always have an ACTIVE frame but are desktop furniture, not what Almir is using.
_NOT_THE_USER = frozenset({
    "gnome-shell", "mutter", "kwin", "kwin_x11", "xfwm4", "xfce4-session",
    "xfsettingsd", "xfdesktop", "xfce4-panel", "openbox", "marco",
    "gjs", "ibus-extension-gtk3", "xdg-desktop-portal", "xdg-desktop-portal-gnome",
    "xdg-desktop-portal-gtk", "gsd-power", "gsd-media-keys", "gsd-color",
    "evolution-alarm-notify", "update-notifier",
})

# A short inline script: find the focused application's name, its top frame title, and PID.
# Exits 0 with JSON always; {"ok": false, ...} on any failure. Never crashes the caller.
_ATSPI_ACTIVE_SCRIPT = """\
import json, sys
try:
    import gi; gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi
    Atspi.init()
    desktop = Atspi.get_desktop(0)
    active = Atspi.StateType.ACTIVE
    focused = Atspi.StateType.FOCUSED
    _SKIP = {
        "gnome-shell", "mutter", "kwin", "xfwm4", "xfce4-session",
        "xfsettingsd", "xfdesktop", "xfce4-panel", "openbox", "marco",
        "gjs", "ibus-extension-gtk3", "xdg-desktop-portal",
        "xdg-desktop-portal-gnome", "xdg-desktop-portal-gtk",
        "gsd-power", "gsd-media-keys", "gsd-color",
        "evolution-alarm-notify", "update-notifier",
    }
    result = {"ok": False, "app": None, "title": None, "pid": None}
    n = desktop.get_child_count()
    apps = []
    for i in range(n):
        try:
            a = desktop.get_child_at_index(i)
            if a is not None:
                apps.append(a)
        except Exception:
            continue

    def frame_is_active(app):
        for j in range(app.get_child_count()):
            try:
                frame = app.get_child_at_index(j)
                if frame is None:
                    continue
                ss = frame.get_state_set()
                if ss.contains(active) or ss.contains(focused):
                    return frame
            except Exception:
                continue
        return None

    # Pass 1: any non-furniture app with an ACTIVE/FOCUSED frame
    for app in apps:
        try:
            name = str(app.get_name() or "").lower()
        except Exception:
            name = ""
        if name in _SKIP:
            continue
        frame = frame_is_active(app)
        if frame is None:
            continue
        try:
            pid = int(app.get_process_id())
        except Exception:
            pid = None
        result = {"ok": True,
                  "app": str(app.get_name() or name),
                  "title": str(frame.get_name() or ""),
                  "pid": pid}
        break

    # Pass 2: check FOCUSED state on individual frames for apps we skipped
    if not result["ok"]:
        for app in apps:
            try:
                name = str(app.get_name() or "").lower()
            except Exception:
                continue
            for j in range(app.get_child_count()):
                try:
                    frame = app.get_child_at_index(j)
                    if frame is None:
                        continue
                    if frame.get_state_set().contains(focused):
                        try:
                            pid = int(app.get_process_id())
                        except Exception:
                            pid = None
                        result = {"ok": True,
                                  "app": str(app.get_name() or name),
                                  "title": str(frame.get_name() or ""),
                                  "pid": pid}
                        break
                except Exception:
                    continue
            if result["ok"]:
                break

    print(json.dumps(result))
except Exception as exc:
    print(json.dumps({"ok": False, "detail": str(exc)[:200]}))
"""


def _dbus_env() -> dict[str, str]:
    """Environment for the system python3 AT-SPI subprocess.
    AT-SPI uses the session D-Bus; Wayland/GNOME requires XDG_RUNTIME_DIR."""
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":0")
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus")
    env.setdefault("ACCESSIBILITY_ENABLED", "1")
    # Remove DISPLAY if it points to a stale XWayland auth we can't satisfy — the Xlib
    # "Authorization required" warning pollutes stderr and misleads the model.
    if not shutil.which("xdotool"):
        env.pop("DISPLAY", None)
    return env


def set_system_python(path: str) -> None:
    """Called once at startup from PerceptionSettings.system_python."""
    global _SYSTEM_PYTHON  # noqa: PLW0603
    _SYSTEM_PYTHON = path


def _read_atspi() -> WindowInfo | None:
    """AT-SPI/Wayland path. Returns WindowInfo for the focused app, or None if unavailable."""
    try:
        out = subprocess.run(  # noqa: S603 - our own inline script, Sali's own machine
            [_SYSTEM_PYTHON, "-c", _ATSPI_ACTIVE_SCRIPT],
            env=_dbus_env(), timeout=6, check=True,
            capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        data = json.loads(out.stdout.strip() or "{}")
    except (json.JSONDecodeError, ValueError):
        return None
    if not data.get("ok"):
        return None
    app = str(data.get("app") or "unknown")
    title = str(data.get("title") or "")
    pid_val = data.get("pid")
    pid = int(pid_val) if isinstance(pid_val, (int, float)) and pid_val > 0 else None
    return WindowInfo(app=app, title=redact(title), pid=pid, geometry=None)


# ── Public entry point ────────────────────────────────────────────────────────────────────────────

async def active_window() -> WindowInfo | None:
    """The focused window. Tries X11 (xdotool) first; falls back to AT-SPI/Wayland.
    Never raises — returns None when there is no readable desktop session."""
    result = await asyncio.to_thread(_read_x11)
    if result is not None:
        return result
    return await asyncio.to_thread(_read_atspi)
