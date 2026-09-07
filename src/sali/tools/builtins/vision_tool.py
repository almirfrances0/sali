"""see_screen — Sali looks at Almir's screen and reasons over it locally (sali3 §33-35).

Vision is the second perception layer: used on demand ("what am I looking at?") or when semantic
info isn't enough. A screenshot is captured (the active window by default — not the whole 34" screen,
§34), reasoned over by the LOCAL vision model, and the RAW frame is discarded immediately (§30) —
only the text observation comes back. The image never leaves the machine (§24-25): no upload, no log.

On the ultrawide the frame reaches the model at NATIVE 3440x1440 with no downscale — measured, and
worth protecting: a "resize to 1024" optimisation would be the one change that destroys small text.
What it costs instead is TIME and CONTEXT: ~27s of image encoding and ~4,400 tokens, 18% of the
working window, for a single whole-screen look. Hence `region`: take one wide look to find the thing,
then crop to read it. A window-target look is ~2.2s and ~200 tokens, which is why it stays the default.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from sali.core.enums import Capability, RiskLevel
from sali.tools.base import Tool, ToolResult
from sali.tools.context import ToolContext
from sali.tools.registry import ToolRegistry

_MAX = 8 * 1024


def _desktop_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":0")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus")
    return env


def _capture(target: str, region: Any = None) -> bytes | None:
    """Grab a screenshot to a temp file, read it, then DELETE the file (never keep the raw frame).
    Returns PNG bytes, or None if there's no display / screenshot tool. `region` crops to
    [x, y, w, h] before the frame is read, so only the part worth looking at is ever loaded."""
    fd, path = tempfile.mkstemp(prefix="sali-shot-", suffix=".png")
    os.close(fd)
    try:
        argv = _capture_argv(target, path)
        if argv is None:
            return None
        subprocess.run(  # noqa: S603 - fixed argv, Sali's own machine
            argv, env=_desktop_env(), timeout=15, check=True, capture_output=True)
        if region:
            _crop(path, region)   # best-effort: an unusable region just means the full frame
        data = Path(path).read_bytes()
        return data or None
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)  # discard the raw frame (§30)


def _capture_argv(target: str, path: str) -> list[str] | None:
    # Active window by default (the primary target), or the current monitor for "screen".
    if shutil.which("spectacle"):
        return ["spectacle", "-b", "-n", "-a" if target == "window" else "-m", "-o", path]
    if shutil.which("xfce4-screenshooter"):
        return ["xfce4-screenshooter", "-w" if target == "window" else "-f", "-s", path]
    # ImageMagick is the floor: no desktop-environment dependency, present on any X install, and the
    # same binary used for cropping below. Without it, a box with neither KDE nor XFCE tooling could
    # not look at its own screen at all.
    if shutil.which("import"):
        return ["import", "-silent", "-window", "root", path]
    return None


def _crop(path: str, region: Any) -> bool:
    """Crop in place to (x, y, w, h). True if the frame now shows only that region.

    Almir's monitor is 3440x1440 — 2.4x wider than tall. A full frame costs ~27s of encoding and
    4,400 tokens (18% of the working context) to look at ONCE, and the thing worth reading is
    usually a few hundred pixels of it. Cropping first is ~0.3s and turns an expensive glance into
    a cheap one that can actually be repeated.
    """
    if not isinstance(region, (list, tuple)) or len(region) != 4:
        return False
    try:
        x, y, w, h = (int(v) for v in region)
    except (TypeError, ValueError):
        return False
    if w <= 0 or h <= 0:
        return False
    tool = shutil.which("magick") or shutil.which("convert")
    if tool is None:
        return False
    try:
        subprocess.run([tool, path, "-crop", f"{w}x{h}+{x}+{y}", "+repage", path],  # noqa: S603
                       check=True, capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


class SeeScreen(Tool):
    name = "see_screen"
    description = (
        "Look at Almir's screen and answer about what's shown — a screenshot is captured and you "
        "reason over it with your vision, all locally. Use it when Almir asks what he's looking at, "
        "or when you need to SEE something (a chart, an error, a UI) that text/state can't tell you. "
        "Defaults to the active window; set target='screen' for the whole current monitor."
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "What to look for / the question about the screen."},
            "target": {"type": "string", "enum": ["window", "screen"],
                       "description": "Active window (default) or the current monitor."},
            "region": {
                "type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4,
                "description": ("Optional [x, y, width, height] to look at just part of the screen. "
                                "The monitor is 3440x1440, so a whole-screen look is slow and "
                                "coarse — take one to find where something is, then use region to "
                                "read it closely."),
            },
        },
        "required": [],
    }
    risk_level = RiskLevel.R1  # reading Almir's own screen for him — benign, stays local
    capabilities = frozenset({Capability.EXECUTE})
    idempotent = False

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.vision is None:
            return ToolResult(ok=False, display="no vision", error="the vision model isn't available")
        target = str(args.get("target") or "window")
        region = args.get("region")
        image = _capture(target, region)
        if image is None:
            return ToolResult(ok=False, display="couldn't capture",
                              error="couldn't take a screenshot — no desktop session or screenshot "
                                    "tool (needs spectacle/xfce4-screenshooter + a display)")
        prompt = str(args.get("prompt") or "").strip() or (
            "Describe what's on the screen: the application, what's visible, and anything notable "
            "(errors, dialogs, warnings). Be concise and concrete.")
        observation = await ctx.vision.look(prompt, image)  # image → LOCAL model only; then dropped
        return ToolResult(
            ok=True,
            output={"observation": observation[:_MAX], "target": target,
                    **({"region": list(region)} if region else {})},
            display="looked at the screen",
        )


def register_builtins(registry: ToolRegistry) -> None:
    registry.register(SeeScreen())
