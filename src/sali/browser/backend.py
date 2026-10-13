"""PlaywrightFirefox — the real browser backend (§44).

Runs Sali's OWN headless Firefox via Playwright (async API only — the sync API's greenlet path is
unsafe here). On first use it imports Almir's Firefox session cookies so Sali is logged in where he
is. Everything is lazy: importing/launching only happens on the first tool call, and if Playwright or
its Firefox isn't installed the tool gets a clear BrowserUnavailable telling Almir what to run.
"""

from __future__ import annotations

import contextlib
from typing import Any

from sali.browser.cookies import firefox_cookies_path, read_firefox_cookies
from sali.browser.port import BrowserUnavailable, PageView

_MAX_TEXT = 40 * 1024
_INSTALL = "pip install 'sali[browser]' && playwright install firefox"


def _guard_scheme(url: str) -> None:
    # Only http(s). Block file://, chrome://, view-source: — an exfiltration / local-read vector.
    if not url.lower().startswith(("http://", "https://")):
        raise BrowserUnavailable(f"refusing to open a non-http(s) URL: {url}")


class PlaywrightFirefox:
    def __init__(self, *, headless: bool = True, firefox_profile: str | None = None,
                 import_cookies: bool = True) -> None:
        self._headless = headless
        self._profile = firefox_profile
        self._import_cookies = import_cookies
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None

    async def _ensure(self) -> None:
        if self._page is not None:
            return
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise BrowserUnavailable(f"browser needs Playwright — run: {_INSTALL}") from exc
        self._pw = await async_playwright().start()
        try:
            self._browser = await self._pw.firefox.launch(headless=self._headless)
        except Exception as exc:  # noqa: BLE001 - Firefox not downloaded yet, or a system-lib gap
            await self._pw.stop()
            self._pw = None
            raise BrowserUnavailable(f"couldn't launch Firefox — run: {_INSTALL} ({exc})") from exc
        self._context = await self._browser.new_context()
        if self._import_cookies:
            await self._seed_cookies()
        self._page = await self._context.new_page()

    async def _seed_cookies(self) -> None:
        path = firefox_cookies_path(self._profile)
        if path is None:
            return
        with contextlib.suppress(Exception):  # logged-in sessions are a bonus, never required
            await self._context.add_cookies(read_firefox_cookies(path))

    async def open(self, url: str) -> PageView:
        _guard_scheme(url)
        await self._ensure()
        await self._page.goto(url, wait_until="domcontentloaded")
        return await self.read()

    async def read(self) -> PageView:
        await self._ensure()
        text = await self._page.inner_text("body")
        return PageView(self._page.url, await self._page.title(), text[:_MAX_TEXT])

    async def click(self, selector: str) -> PageView:
        await self._ensure()
        await self._page.click(selector)
        return await self.read()

    async def fill(self, selector: str, value: str) -> PageView:
        await self._ensure()
        await self._page.fill(selector, value)
        return await self.read()

    async def screenshot(self, path: str) -> str:
        await self._ensure()
        await self._page.screenshot(path=path)
        return path

    async def aclose(self) -> None:
        for closer in (self._context, self._browser):
            if closer is not None:
                with contextlib.suppress(Exception):
                    await closer.close()
        if self._pw is not None:
            with contextlib.suppress(Exception):
                await self._pw.stop()
        self._pw = self._browser = self._context = self._page = None
