"""DesktopPerception — composes active-window (X11 or AT-SPI) and accessibility (AT-SPI) backends.

The window signal is cheap and (almost) always present; the UI tree is best-effort and only fetched
when asked. `snapshot` never raises: a fully unusable desktop returns ``available=False`` with a
human-readable ``detail``. Reused by the on-demand tool and by the continuous perception loop, which
asks for the tree only when the focused window CHANGES — the probe costs ~86ms, which is far too much
every 3 seconds and nothing at all once per app switch.

On GNOME Wayland (Ubuntu 26.04+) xdotool is absent and DISPLAY/XWayland auth is session-specific.
The activewindow module now has two paths: X11 (xdotool, unchanged) and AT-SPI (Wayland native,
no X11 deps). The system_python setting is forwarded to activewindow at construction time so the
AT-SPI subprocess knows which interpreter to call.
"""

from __future__ import annotations

from typing import Any

from sali.obs.log import get_logger
from sali.perception import activewindow, atspi
from sali.perception.base import Perception


def _is_empty_tree(tree: dict[str, Any]) -> bool:
    """True when the tree describes the desktop's own furniture rather than Almir's application."""
    name = str(tree.get("name") or "").lower()
    if name in {"xfwm4", "xfce4-session", "xfsettingsd", "mutter", "kwin", "kwin_x11", "openbox"}:
        return True
    kids = tree.get("children")
    return not isinstance(kids, list) or len(kids) == 0


class DesktopPerception:
    def __init__(self, settings: Any) -> None:
        self.s = settings
        self._log = get_logger("sali.perception")
        # Forward system_python to activewindow so the AT-SPI/Wayland fallback knows
        # which interpreter has gi bindings (Sali's 3.14 venv does not).
        _sp = str(getattr(settings, "system_python", "/usr/bin/python3") or "/usr/bin/python3")
        activewindow.set_system_python(_sp)

    async def snapshot(self, *, ui: bool = False) -> dict[str, Any]:
        window = await activewindow.active_window()
        ui_dict: dict[str, Any] | None = None
        detail = "window only"
        if ui:
            node, detail = await atspi.ui_tree(
                self.s.system_python, max_depth=self.s.max_tree_depth,
                max_nodes=self.s.max_tree_nodes,
                pid=int(getattr(window, "pid", 0) or 0))
            ui_dict = node.to_dict() if node is not None else None
            # A tree of the window manager, or a bare stub with no children, is not an accessibility
            # reading — it is the absence of one. This reported detail="ok" while returning a 2-node
            # xfwm4 husk on every single call, so nothing downstream (health, diagnostics, Sali
            # himself) had any way to notice the accessibility layer was dark.
            if ui_dict is not None and _is_empty_tree(ui_dict):
                app_name = str(ui_dict.get("name") or ui_dict.get("role") or "?")
                ui_dict, detail = None, (
                    f"accessibility returned nothing usable (got '{app_name}'): the focused app "
                    f"isn't exporting a tree")
        available = window is not None or ui_dict is not None
        if not available and not ui:
            detail = ("no focused window — xdotool absent and AT-SPI returned nothing "
                      "(check XDG_RUNTIME_DIR, DBUS_SESSION_BUS_ADDRESS, ACCESSIBILITY_ENABLED)")
        return {
            "available": available,
            "window": window.to_dict() if window is not None else None,
            "ui": ui_dict,
            "detail": detail,
        }


def build_perception(settings: Any) -> Perception:
    """Wire the perception backend from settings ('fake' for tests, else the live desktop)."""
    if getattr(settings, "perception", None) is not None:
        settings = settings.perception
    if getattr(settings, "backend", "desktop") == "fake":
        from sali.perception.fake import FakePerception

        return FakePerception()
    return DesktopPerception(settings)
