"""Browser automation (spec §44): Sali's own headless Firefox, seeded with Almir's login cookies."""

from sali.browser.port import Browser, BrowserUnavailable, PageView
from sali.browser.service import build_browser

__all__ = ["Browser", "BrowserUnavailable", "PageView", "build_browser"]
