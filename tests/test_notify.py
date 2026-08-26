"""The notify tool (§44): Sali reaching out to Almir proactively."""

from __future__ import annotations

from typing import Any

import pytest

from sali.config.settings import Settings
from sali.core.clock import SystemClock
from sali.tools.builtins.notify_tool import Notify, _desktop_env, _pick_terminal
from sali.tools.context import ToolContext


def _ctx() -> ToolContext:
    return ToolContext(settings=Settings(), clock=SystemClock())


def test_desktop_env_falls_back_to_the_user_session(monkeypatch: pytest.MonkeyPatch) -> None:
    # A background daemon has no DISPLAY/DBUS — the tool supplies the logged-in session's, so the
    # notification actually reaches the desktop.
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    env = _desktop_env()
    assert env["DISPLAY"] == ":0"
    assert env["DBUS_SESSION_BUS_ADDRESS"].startswith("unix:path=/run/user/")


async def test_notify_requires_a_message() -> None:
    res = await Notify().run({"message": "   "}, _ctx())
    assert not res.ok and "message" in (res.error or "")


def test_pick_terminal_finds_one_on_this_box() -> None:
    assert _pick_terminal() is not None  # konsole/qterminal/x-terminal-emulator are installed


class _Ok:
    returncode = 0


async def test_notify_sends_notification_and_opens_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, list[str]] = {}

    def fake_run(argv: list[str], **kw: Any) -> _Ok:
        seen["run"] = argv
        return _Ok()

    def fake_popen(argv: list[str], **kw: Any) -> object:
        seen["popen"] = argv
        return object()

    monkeypatch.setattr("sali.tools.builtins.notify_tool.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("sali.tools.builtins.notify_tool.subprocess.run", fake_run)
    monkeypatch.setattr("sali.tools.builtins.notify_tool.subprocess.Popen", fake_popen)

    res = await Notify().run({"message": "backup finished", "terminal": True}, _ctx())
    assert res.ok and "terminal" in res.output["delivered"]
    assert seen["run"][0] == "notify-send" and "backup finished" in seen["run"]
    assert seen["popen"][0]  # a terminal window was opened with the full message


async def test_notify_reports_when_no_desktop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sali.tools.builtins.notify_tool.shutil.which", lambda name: None)
    res = await Notify().run({"message": "hi"}, _ctx())
    assert not res.ok and "desktop" in (res.error or "")
