"""Browser automation (§44): Firefox cookie import, the fake, build, tools, and the guards."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sali.browser.backend import PlaywrightFirefox
from sali.browser.cookies import firefox_cookies_path, read_firefox_cookies
from sali.browser.fake import FakeBrowser
from sali.browser.port import BrowserUnavailable, PageView
from sali.browser.service import build_browser
from sali.config.settings import BrowserSettings, Settings
from sali.core.clock import SystemClock
from sali.core.enums import RiskLevel
from sali.tools.builtins.browser_tool import (
    BrowserClick,
    BrowserFill,
    BrowserOpen,
    BrowserScreenshot,
)
from sali.tools.context import ToolContext


# ---- Firefox cookie import (the "use my Gmail logins" bridge) -----------------------------------
def _make_cookies_db(path: Path) -> None:
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE moz_cookies (host TEXT, name TEXT, value TEXT, path TEXT, "
                "expiry INTEGER, isSecure INTEGER, isHttpOnly INTEGER, sameSite INTEGER)")
    con.execute("INSERT INTO moz_cookies VALUES "
                "('mail.google.com','SID','tok','/',9999999999,1,1,2)")  # sameSite 2 = Strict
    con.execute("INSERT INTO moz_cookies VALUES ('x.com','s','v','/',0,0,0,0)")  # session cookie
    con.commit()
    con.close()


def test_read_firefox_cookies_maps_to_playwright(tmp_path: Path) -> None:
    db = tmp_path / "cookies.sqlite"
    _make_cookies_db(db)
    cookies = read_firefox_cookies(db)
    sid = next(c for c in cookies if c["name"] == "SID")
    assert sid["domain"] == "mail.google.com" and sid["value"] == "tok"
    assert sid["secure"] and sid["httpOnly"] and sid["sameSite"] == "Strict"
    assert sid["expires"] == 9999999999
    session = next(c for c in cookies if c["name"] == "s")
    assert "expires" not in session and session["sameSite"] == "None"  # expiry 0 → session cookie


def test_firefox_cookies_path_explicit_and_missing(tmp_path: Path) -> None:
    profile = tmp_path / "abc.default-release"
    profile.mkdir()
    _make_cookies_db(profile / "cookies.sqlite")
    assert firefox_cookies_path(str(profile)) == profile / "cookies.sqlite"
    assert firefox_cookies_path(str(tmp_path / "nope")) is None


# ---- the fake + build ---------------------------------------------------------------------------
async def test_fake_browser_records_actions() -> None:
    fake = FakeBrowser({"https://x.com": PageView("https://x.com", "X", "hello")})
    view = await fake.open("https://x.com")
    assert view.title == "X" and view.text == "hello"
    await fake.fill("#q", "cats")
    await fake.click("#go")
    assert ("fill", "#q", "cats") in fake.actions and ("click", "#go") in fake.actions


def test_build_browser_selects_backend() -> None:
    assert isinstance(build_browser(BrowserSettings(backend="fake")), FakeBrowser)
    assert isinstance(build_browser(BrowserSettings(backend="playwright")), PlaywrightFirefox)


# ---- scheme guard (runs BEFORE any Playwright import, so testable with nothing installed) --------
async def test_backend_refuses_non_http_scheme() -> None:
    with pytest.raises(BrowserUnavailable):
        await PlaywrightFirefox().open("file:///etc/passwd")  # exfiltration vector — blocked


# ---- tools --------------------------------------------------------------------------------------
def _ctx(browser: FakeBrowser) -> ToolContext:
    return ToolContext(settings=Settings(), clock=SystemClock(), browser=browser)


async def test_browser_tools_open_fill_click() -> None:
    fake = FakeBrowser()
    ctx = _ctx(fake)
    opened = await BrowserOpen().run({"url": "https://example.com"}, ctx)
    assert opened.ok and opened.output["url"] == "https://example.com"
    filled = await BrowserFill().run({"selector": "#q", "value": "hi"}, ctx)
    assert filled.ok
    clicked = await BrowserClick().run({"selector": ".link"}, ctx)
    assert clicked.ok


def test_state_changing_click_escalates_to_r4() -> None:
    assert BrowserClick().assess({"selector": ".menu-item"}) is RiskLevel.R1
    for danger in ("button.buy", "#checkout", "input[type=submit]", ".send-money"):
        assert BrowserClick().assess({"selector": danger}) is RiskLevel.R4


async def test_screenshot_is_path_confined(tmp_path: Path) -> None:
    from sali.config.settings import PermissionsSettings

    fake = FakeBrowser()
    perms = PermissionsSettings(fs_read_roots=[str(tmp_path)], fs_write_roots=[str(tmp_path)])
    ctx = ToolContext(settings=Settings(permissions=perms), clock=SystemClock(), browser=fake)
    ok = await BrowserScreenshot().run({"path": str(tmp_path / "shot.png")}, ctx)
    assert ok.ok
    denied = await BrowserScreenshot().run({"path": "/root/shot.png"}, ctx)
    assert not denied.ok and "denied" in denied.display
