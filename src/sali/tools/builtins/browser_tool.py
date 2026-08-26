"""Browser tools — Sali drives its own headless Firefox (§44).

Navigating, reading the page, typing, and screenshotting run FREE. A CLICK that looks like it commits
something (submit / buy / checkout / pay / send / confirm / transfer / delete) crosses the trust
boundary → R4, one confirm, so an unattended run can't buy or send anything. The browser is seeded
with Almir's Firefox login cookies, so it's already signed in where he is.
"""

from __future__ import annotations

import re
from typing import Any

from sali.core.enums import Capability, RiskLevel
from sali.tools.base import Tool, ToolResult
from sali.tools.context import ToolContext
from sali.tools.pathguard import PathViolation
from sali.tools.registry import ToolRegistry

_STATE_CHANGING = re.compile(
    r"(?i)(submit|buy|checkout|pay|order|send|confirm|transfer|delete|purchase|subscribe)")
_MAX = 32 * 1024


def _view(res: Any) -> dict[str, Any]:
    return {"url": res.url, "title": res.title, "text": res.text[:_MAX]}


def _err(exc: Exception) -> str:
    return str(exc) or exc.__class__.__name__


class BrowserOpen(Tool):
    name = "browser_open"
    description = ("Open a web page in your browser and return its readable text. Only http/https. "
                  "You're already logged into sites Almir is logged into.")
    parameters = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}
    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.NETWORK})

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.browser is None:
            return ToolResult(ok=False, display="no browser", error="the browser isn't available")
        try:
            view = await ctx.browser.open(str(args.get("url", "")))
        except Exception as exc:  # noqa: BLE001 - not-installed / bad-url / nav error → report it
            return ToolResult(ok=False, display="open failed", error=_err(exc))
        return ToolResult(ok=True, output=_view(view), display=view.summary())


class BrowserRead(Tool):
    name = "browser_read"
    description = "Read the current page's text again (after it changed)."
    parameters = {"type": "object", "properties": {}, "required": []}
    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.NETWORK})

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.browser is None:
            return ToolResult(ok=False, display="no browser", error="the browser isn't available")
        try:
            view = await ctx.browser.read()
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, display="read failed", error=_err(exc))
        return ToolResult(ok=True, output=_view(view), display=view.summary())


class BrowserClick(Tool):
    name = "browser_click"
    description = ("Click an element by CSS selector. A click that commits something (submit, buy, "
                  "send, delete…) pauses to confirm first.")
    parameters = {"type": "object", "properties": {"selector": {"type": "string"}},
                  "required": ["selector"]}
    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.NETWORK})
    idempotent = False

    def assess(self, args: dict[str, Any]) -> RiskLevel:
        return RiskLevel.R4 if _STATE_CHANGING.search(str(args.get("selector", ""))) else RiskLevel.R1

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.browser is None:
            return ToolResult(ok=False, display="no browser", error="the browser isn't available")
        try:
            view = await ctx.browser.click(str(args.get("selector", "")))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, display="click failed", error=_err(exc))
        return ToolResult(ok=True, output=_view(view), display=f"clicked; {view.summary()}")


class BrowserFill(Tool):
    name = "browser_fill"
    description = "Type a value into a form field by CSS selector."
    parameters = {
        "type": "object",
        "properties": {"selector": {"type": "string"}, "value": {"type": "string"}},
        "required": ["selector", "value"],
    }
    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.NETWORK})
    idempotent = False

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.browser is None:
            return ToolResult(ok=False, display="no browser", error="the browser isn't available")
        try:
            view = await ctx.browser.fill(str(args.get("selector", "")), str(args.get("value", "")))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, display="fill failed", error=_err(exc))
        return ToolResult(ok=True, output=_view(view), display="filled")


class BrowserScreenshot(Tool):
    name = "browser_screenshot"
    description = "Save a screenshot of the current page to a local file path."
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.NETWORK, Capability.WRITE})
    idempotent = False

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.browser is None:
            return ToolResult(ok=False, display="no browser", error="the browser isn't available")
        try:
            path = ctx.paths.check_write(args["path"])  # confine the screenshot to allowed roots
        except PathViolation as exc:
            return ToolResult(ok=False, display="denied", error=str(exc))
        try:
            saved = await ctx.browser.screenshot(str(path))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, display="screenshot failed", error=_err(exc))
        return ToolResult(ok=True, output={"path": saved}, display=f"saved {path.name}")


def register_builtins(registry: ToolRegistry) -> None:
    for tool in (BrowserOpen(), BrowserRead(), BrowserClick(), BrowserFill(), BrowserScreenshot()):
        registry.register(tool)
