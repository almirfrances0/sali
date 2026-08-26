"""Web / online tools (spec §9-12): explicit, provenance-carrying internet access.

Online access is a first-class *tool* category, deliberately NOT the same as shell networking —
the model calls ``web_search`` / ``web_fetch`` when they help. Every result carries provenance
(source URL, domain, title, retrieval time) so Sali treats what it finds as "reported by a source
at a time", never as something it observed first-hand. These are read-only tool results: they
live in the turn's working context and never auto-persist (§11) — stale web facts can't leak into
permanent memory on their own. Pair them with local inspection for grounded understanding (§12).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from html import unescape
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from sali.core.enums import Capability, RiskLevel
from sali.tools.base import Tool, ToolResult
from sali.tools.context import ToolContext
from sali.tools.registry import ToolRegistry

_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
_MAX_TEXT = 32_000  # capture most of a page (the loop caps what the model sees); was a tight 8000
_TIMEOUT = 20.0

_SCRIPT = re.compile(r"<(script|style|noscript|template)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_INLINE_WS = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n\s*\n\s*")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_RESULT_A = re.compile(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                       re.IGNORECASE | re.DOTALL)
_SNIPPET = re.compile(r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>',
                      re.IGNORECASE | re.DOTALL)


def _strip_tags(fragment: str) -> str:
    return unescape(_TAG.sub("", fragment)).strip()


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except ValueError:
        return ""


def html_to_text(html: str) -> str:
    """A deterministic HTML → readable text pass: drop scripts/styles, strip tags, unescape,
    and collapse whitespace. Good enough to read documentation; not a full renderer."""
    body = _SCRIPT.sub(" ", html)
    body = _TAG.sub(" ", body)
    body = unescape(body)
    body = _INLINE_WS.sub(" ", body)
    body = _BLANK_LINES.sub("\n", body)
    return body.strip()


def title_of(html: str) -> str:
    match = _TITLE.search(html)
    return _strip_tags(match.group(1)) if match else ""


def real_url(href: str) -> str:
    """DuckDuckGo wraps result links as //duckduckgo.com/l/?uddg=<encoded> — unwrap to the target."""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        uddg = parse_qs(parsed.query).get("uddg")
        if uddg:
            return unquote(uddg[0])
    return href


def parse_ddg(html: str) -> list[dict[str, str]]:
    """Parse DuckDuckGo's HTML results into (title, url, domain, snippet), most-relevant first."""
    snippets = [_strip_tags(s)[:300] for s in _SNIPPET.findall(html)]
    out: list[dict[str, str]] = []
    for i, (href, title) in enumerate(_RESULT_A.findall(html)):
        url = real_url(href)
        out.append({
            "title": _strip_tags(title), "url": url, "domain": _domain(url),
            "snippet": snippets[i] if i < len(snippets) else "",
        })
    return out


class WebFetch(Tool):
    name = "web_fetch"
    description = (
        "Fetch a web page (http/https) and return its readable text, with provenance (final url, "
        "title, when it was retrieved). Use it to read docs, changelogs, issues, and articles."
    )
    parameters = {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
    }
    risk_level = RiskLevel.R1  # read-only network; online is a normal capability (§9)
    capabilities = frozenset({Capability.NETWORK})

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        url = str(args.get("url", "")).strip()
        if not url.lower().startswith(("http://", "https://")):
            return ToolResult(ok=False, display="bad url",
                              error="web_fetch takes an http:// or https:// URL")
        try:
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=_TIMEOUT, headers={"User-Agent": _UA}
            ) as client:
                resp = await client.get(url)
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, display="fetch failed", error=str(exc)[:200])
        is_html = "html" in resp.headers.get("content-type", "").lower()
        content = (html_to_text(resp.text) if is_html else resp.text)[:_MAX_TEXT]
        final = str(resp.url)
        return ToolResult(
            ok=resp.is_success,
            output={
                "url": final, "domain": _domain(final), "title": title_of(resp.text),
                "retrieved_at": datetime.now(UTC).isoformat(), "status": resp.status_code,
                "content": content,
            },
            display=f"fetched {_domain(final)} ({resp.status_code})",
            error=None if resp.is_success else f"HTTP {resp.status_code}",
        )


class WebSearch(Tool):
    name = "web_search"
    description = (
        "Search the web and return the top results (title, url, domain, snippet) with provenance. "
        "Use it to find current info, docs, versions, CVEs, and answers you don't already know."
    )
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}},
        "required": ["query"],
    }
    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.NETWORK})

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolResult(ok=False, display="no query", error="web_search needs a query")
        limit = min(int(args.get("max_results") or 6), 10)
        try:
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=_TIMEOUT, headers={"User-Agent": _UA}
            ) as client:
                resp = await client.post("https://html.duckduckgo.com/html/", data={"q": query})
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, display="search failed", error=str(exc)[:200])
        results = parse_ddg(resp.text)[:limit]
        return ToolResult(
            ok=bool(results),
            output={"query": query, "retrieved_at": datetime.now(UTC).isoformat(),
                    "results": results},
            display=f"{len(results)} result(s) for {query!r}",
            error=None if results else "no results (search may be rate-limited — try web_fetch)",
        )


def register_builtins(registry: ToolRegistry) -> None:
    registry.register(WebSearch())
    registry.register(WebFetch())
