"""The Browser port — the interface every backend (real Playwright-Firefox, or the CI fake) meets.

Kept small and engine-agnostic so a FakeBrowser can stand in for CI with no subprocess, and the real
backend can be swapped without touching the tools. State-changing actions are separated from reads so
the trust boundary (a submit/navigation that changes remote state → confirm) is enforceable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(slots=True)
class PageView:
    url: str
    title: str
    text: str  # the readable text of the rendered page (not raw HTML)

    def summary(self) -> str:
        return f"{self.title} — {self.url}"


class Browser(Protocol):
    async def open(self, url: str) -> PageView: ...
    async def read(self) -> PageView: ...
    async def click(self, selector: str) -> PageView: ...
    async def fill(self, selector: str, value: str) -> PageView: ...
    async def screenshot(self, path: str) -> str: ...
    async def aclose(self) -> None: ...


class BrowserUnavailable(RuntimeError):
    """The real browser can't run yet (Playwright/Firefox not installed) — carries the fix."""
