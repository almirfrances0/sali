"""FakeBrowser — an in-memory browser for CI. No subprocess, no network, records every action."""

from __future__ import annotations

from sali.browser.port import PageView


class FakeBrowser:
    def __init__(self, pages: dict[str, PageView] | None = None) -> None:
        self._pages = pages or {}
        self._current: PageView | None = None
        self.actions: list[tuple[str, ...]] = []

    async def open(self, url: str) -> PageView:
        self.actions.append(("open", url))
        self._current = self._pages.get(url, PageView(url, f"Page at {url}", f"[fake content] {url}"))
        return self._current

    async def read(self) -> PageView:
        return self._current or PageView("about:blank", "", "")

    async def click(self, selector: str) -> PageView:
        self.actions.append(("click", selector))
        return await self.read()

    async def fill(self, selector: str, value: str) -> PageView:
        self.actions.append(("fill", selector, value))
        return await self.read()

    async def screenshot(self, path: str) -> str:
        self.actions.append(("screenshot", path))
        return path

    async def aclose(self) -> None:
        self.actions.append(("close",))
