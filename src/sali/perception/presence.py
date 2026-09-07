"""Is Almir at the machine right now, and has he just paused?

Sali had no idea. Every judgement about when to speak was made from "did a message arrive recently",
which says nothing about whether Almir is sitting there. Someone next to you doesn't talk over your
typing and doesn't talk to an empty chair — they speak in the gap. This is the signal that makes that
possible, and it costs nothing: the X server has been counting idle time all along.

ctypes against libXss, deliberately: python3-xlib and xprintidle are both absent from this box and
neither is needed. Every failure path returns None, meaning "unknown" — never a fabricated presence.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
from typing import Any

from sali.obs.log import get_logger

log = get_logger("sali.perception.presence")

# Someone mid-keystroke is not to be interrupted; someone who paused a few seconds is available.
ACTIVE_S = 8.0
# Past this he has walked away. Not a reason to stay silent — his messages persist and he reads them
# later — but it IS the difference between "he's ignoring me" and "he isn't there", which matters a
# great deal to a system that learns from being ignored.
AWAY_S = 600.0

_XSS_INFO = None


class _XScreenSaverInfo(ctypes.Structure):
    _fields_ = [("window", ctypes.c_ulong), ("state", ctypes.c_int), ("kind", ctypes.c_int),
                ("since", ctypes.c_ulong), ("idle", ctypes.c_ulong), ("event_mask", ctypes.c_ulong)]


def _load() -> Any:
    global _XSS_INFO
    if _XSS_INFO is not None:
        return _XSS_INFO
    x11 = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
    xss = ctypes.CDLL(ctypes.util.find_library("Xss") or "libXss.so.1")
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XDefaultRootWindow.restype = ctypes.c_ulong
    x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    xss.XScreenSaverAllocInfo.restype = ctypes.POINTER(_XScreenSaverInfo)
    xss.XScreenSaverQueryInfo.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                          ctypes.POINTER(_XScreenSaverInfo)]
    _XSS_INFO = (x11, xss)
    return _XSS_INFO


def idle_seconds() -> float | None:
    """Seconds since Almir last touched the keyboard or mouse, or None if it cannot be known."""
    try:
        x11, xss = _load()
        display = x11.XOpenDisplay(os.environ.get("DISPLAY", ":0").encode())
        if not display:
            return None
        try:
            root = x11.XDefaultRootWindow(display)
            info = xss.XScreenSaverAllocInfo()
            if not xss.XScreenSaverQueryInfo(display, root, info):
                return None
            return float(info.contents.idle) / 1000.0
        finally:
            x11.XCloseDisplay(display)
    except Exception:  # noqa: BLE001 - no display, no libXss, a locked session: all just "unknown"
        return None


def state() -> str:
    """'working' (hands on the machine), 'here' (present, paused), 'away', or 'unknown'."""
    idle = idle_seconds()
    if idle is None:
        return "unknown"
    if idle < ACTIVE_S:
        return "working"
    return "here" if idle < AWAY_S else "away"


def describe() -> str | None:
    """One line for Sali's world state, or None when there is nothing honest to say.

    Phrased as a fact about THIS MACHINE'S input devices, not about where Almir is. It measures X11
    idle time, and Almir talks to Sali from his phone — so "Almir hasn't touched the machine for 50
    minutes" was simultaneously true and completely misleading. Sali read it as a statement about
    Almir and turned it into "you've been staring at qterminal without typing", "you've been quiet
    for nearly an hour", "since you've been ghosting me for an hour" — four times in one evening, to
    a man who was sitting there messaging him. Idle keyboard is not absence."""
    idle = idle_seconds()
    if idle is None:
        return None
    if idle < ACTIVE_S:
        return "Someone is using this machine's keyboard/mouse right now"
    if idle < AWAY_S:
        return f"This machine's keyboard/mouse has been idle for {int(idle)}s"
    mins = int(idle // 60)
    return (f"This machine's keyboard/mouse has been idle for {mins} minutes "
            "(this says nothing about where Almir is — he may well be on his phone)")
