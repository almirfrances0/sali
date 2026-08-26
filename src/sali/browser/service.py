"""build_browser — the single edge that names a concrete backend (fake in CI, Playwright live)."""

from __future__ import annotations

from typing import Any

from sali.browser.fake import FakeBrowser
from sali.browser.port import Browser


def build_browser(settings: Any) -> Browser:
    if getattr(settings, "backend", "playwright") == "fake":
        return FakeBrowser()
    from sali.browser.backend import PlaywrightFirefox

    return PlaywrightFirefox(
        headless=getattr(settings, "headless", True),
        firefox_profile=getattr(settings, "firefox_profile", None),
        import_cookies=getattr(settings, "import_cookies", True))
