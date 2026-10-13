"""A deterministic perception for tests and pool-less wiring (no desktop, no subprocesses)."""

from __future__ import annotations

from typing import Any

from sali.perception.base import UiNode, WindowInfo


class FakePerception:
    """Returns a scripted snapshot. Nothing here touches X11 or AT-SPI, so the suite never depends
    on a running desktop."""

    def __init__(self, *, window: WindowInfo | None = None, ui: UiNode | None = None) -> None:
        self._window = window if window is not None else WindowInfo(
            app="qterminal", title="almir@kali: ~", pid=4242, geometry=(0, 0, 1920, 1080))
        self._ui = ui if ui is not None else UiNode(
            role="frame", name="almir@kali: ~", children=[UiNode(role="terminal", name="")])

    async def snapshot(self, *, ui: bool = False) -> dict[str, Any]:
        return {
            "available": True,
            "window": self._window.to_dict(),
            "ui": self._ui.to_dict() if (ui and self._ui is not None) else None,
            "detail": "ok" if ui else "window only",
        }
