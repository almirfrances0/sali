"""The see_screen tool (sali3 §33-35): Sali looks at the screen and reasons over it LOCALLY."""

from __future__ import annotations

from typing import Any

import pytest

from sali.config.settings import Settings
from sali.core.clock import SystemClock
from sali.provider.fake import FakeModelProvider
from sali.tools.builtins import vision_tool
from sali.tools.builtins.vision_tool import SeeScreen, _capture_argv
from sali.tools.context import ToolContext


class _Vision:
    """A local vision sink that records what it was asked, so we can assert the image never leaks."""

    def __init__(self) -> None:
        self.seen: dict[str, Any] = {}

    async def look(self, prompt: str, image: bytes) -> str:
        self.seen = {"prompt": prompt, "bytes": len(image)}
        return "a terminal showing a passing test run"


def _ctx(vision: Any = None) -> ToolContext:
    return ToolContext(settings=Settings(), clock=SystemClock(), vision=vision)


def test_capture_argv_prefers_active_window_then_monitor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sali.tools.builtins.vision_tool.shutil.which",
                        lambda name: "/usr/bin/spectacle" if name == "spectacle" else None)
    assert _capture_argv("window", "/tmp/x.png")[:4] == ["spectacle", "-b", "-n", "-a"]  # type: ignore[index]
    assert _capture_argv("screen", "/tmp/x.png")[3] == "-m"  # type: ignore[index]


def test_capture_argv_none_without_a_screenshot_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sali.tools.builtins.vision_tool.shutil.which", lambda name: None)
    assert _capture_argv("window", "/tmp/x.png") is None


async def test_see_screen_needs_vision() -> None:
    res = await SeeScreen().run({}, _ctx(vision=None))
    assert not res.ok and "vision" in (res.error or "")


async def test_see_screen_reports_when_capture_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vision_tool, "_capture", lambda target: None)
    res = await SeeScreen().run({}, _ctx(vision=_Vision()))
    assert not res.ok and "screenshot" in (res.error or "")


async def test_see_screen_describes_locally_and_drops_the_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vision_tool, "_capture", lambda target: b"\x89PNG fake-bytes")
    vis = _Vision()
    res = await SeeScreen().run({"prompt": "what am I running?"}, _ctx(vision=vis))
    assert res.ok
    assert res.output["observation"] == "a terminal showing a passing test run"
    assert res.output["target"] == "window"
    # the image reached the LOCAL sink — and only the text observation comes back (no bytes stored)
    assert vis.seen == {"prompt": "what am I running?", "bytes": len(b"\x89PNG fake-bytes")}
    assert "observation" in res.output and "image" not in res.output


async def test_fake_provider_describe_image_is_local_and_deterministic() -> None:
    out = await FakeModelProvider().describe_image("what is this", b"1234")
    assert out == "[fake vision of 4 bytes] what is this"
