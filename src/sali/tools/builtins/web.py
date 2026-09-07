"""Web / online tools (spec §9-12): explicit, provenance-carrying internet access.

Online access is a first-class *tool* category, deliberately NOT the same as shell networking —
the model calls ``web_search`` / ``web_fetch`` when they help. Every result carries provenance
(source URL, domain, title, retrieval time) so Sali treats what it finds as "reported by a source
at a time", never as something it observed first-hand. These are read-only tool results: they
live in the turn's working context and never auto-persist (§11) — stale web facts can't leak into
permanent memory on their own. Pair them with local inspection for grounded understanding (§12).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
from datetime import UTC, datetime, timedelta
from html import unescape
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import trafilatura

import httpx

from sali.core.enums import Capability, RiskLevel
from sali.tools.base import Tool, ToolResult
from sali.tools.context import ToolContext
from sali.tools.registry import ToolRegistry

_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
# Sized against the REAL working context (ctx_default = 24,576 TOKENS, not 65K chars): one
# nav-heavy page used to be able to spend two-thirds of Sali's context on menus.
_MAX_TEXT = 24_000
_MAX_LINKS = 40
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


_A_HREF = re.compile(r"<a\b[^>]*\bhref=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
                     re.IGNORECASE | re.DOTALL)
_PRIVATE_HOST = re.compile(
    r"^(?:localhost|127\.|0\.0\.0\.0|10\.|192\.168\.|169\.254\.|::1|"
    r"172\.(?:1[6-9]|2\d|3[01])\.)", re.IGNORECASE)


def _is_private_target(url: str) -> bool:
    """Refuse loopback, the LAN and cloud metadata.

    A supervised foreground fetch is one risk profile; a BACKGROUND learner following whatever a search
    result happens to point at, unattended, is another — it must never be able to reach the router's
    admin page or a metadata endpoint. (The browser backend already refuses file:// and chrome:// for
    exactly this reason.)"""
    host = (urlparse(url).hostname or "").strip().lower()
    if not host:
        return True
    return bool(_PRIVATE_HOST.match(host)) or host.endswith(".local")


def _registrable(host: str) -> str:
    """Crude eTLD+1 — enough to keep link-following on the same site without a public-suffix list."""
    parts = (host or "").lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else (host or "")


def extract_links(html: str, base_url: str, limit: int = _MAX_LINKS) -> list[dict[str, str]]:
    """Same-site links with their anchor text, absolutised against the FINAL url.

    `html_to_text` strips every tag — including every <a href> — before returning, so following a
    result into a site's own documentation was PHYSICALLY IMPOSSIBLE: search -> read -> follow -> refine
    could never happen, which is why the web-research skill's "read the index, then the specific page"
    step had never once been executable. Extracting links BEFORE the strip is what makes multi-step
    research real. Same-site only: search is how you leave a domain, not an arbitrary outbound hop."""
    base_host = _registrable(urlparse(base_url).hostname or "")
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for href, anchor in _A_HREF.findall(html or ""):
        href = unescape(href.strip())
        if href.lower().startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
            continue
        absolute = urljoin(base_url, href).split("#", 1)[0]
        if not absolute.lower().startswith(("http://", "https://")):
            continue
        if _registrable(urlparse(absolute).hostname or "") != base_host:
            continue
        if absolute in seen or absolute.rstrip("/") == base_url.rstrip("/"):
            continue
        text = _INLINE_WS.sub(" ", unescape(_TAG.sub(" ", anchor))).strip()
        if not text:
            continue
        seen.add(absolute)
        out.append({"url": absolute, "text": text[:120]})
        if len(out) >= limit:
            break
    return out


def extract_main(html: str, url: str) -> str:
    """Main content as Markdown — headings and fenced code preserved.

    `html_to_text` flattens a page into one undifferentiated river of prose AND keeps every nav/footer
    menu, so Sali could not tell a command from commentary, and a GitHub page could return thousands of
    characters that were 100% global navigation. trafilatura drops the chrome and keeps the article.
    Falls back to the flat reader rather than failing: a rough read beats no read."""
    with contextlib.suppress(Exception):
        text = trafilatura.extract(
            html, url=url, output_format="markdown", include_tables=True,
            include_comments=False, include_links=False, favor_precision=True)
        if text and text.strip():
            return text.strip()
    return html_to_text(html)


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
        "Fetch a web page (http/https) and return its MAIN CONTENT as markdown (headings and code "
        "preserved, navigation stripped), plus same-site `links` you can follow to go deeper, plus "
        "provenance (final url, title, retrieval time). Use it to read docs, changelogs and articles — "
        "search, read, then follow a link to the specific page you actually need."
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
        if _is_private_target(url):
            return ToolResult(ok=False, display="refused",
                              error="web_fetch refuses loopback/LAN/link-local addresses")
        cached = await _cache_get(ctx, "fetch", url)
        if cached is not None:
            return ToolResult(ok=True, output={**cached, "cached": True},
                              display=f"read {cached.get('domain')} (cached)")
        try:
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=_TIMEOUT, headers={"User-Agent": _UA}
            ) as client:
                # StackOverflow walls a plain client, but its API is open — and returns the ANSWERS,
                # which is the part that answers "how do I do this".
                via_api: dict[str, Any] | None = None
                with contextlib.suppress(Exception):
                    via_api = await _fetch_stackoverflow(client, url)
                if via_api is not None:
                    payload = {
                        "url": url, "domain": _domain(url), "title": via_api["title"],
                        "retrieved_at": datetime.now(UTC).isoformat(), "status": 200,
                        "content": via_api["content"][:_MAX_TEXT], "links": [],
                        "via": "api.stackexchange.com",
                    }
                    await _cache_put(ctx, "fetch", url, payload, 200)
                    return ToolResult(
                        ok=True, output=payload,
                        display=f"read {len(payload['content'])} chars of answers "
                                "via api.stackexchange.com")
                resp = await client.get(url)
        except httpx.HTTPError as exc:
            stale = await _stale_fallback(ctx, "fetch", url, f"network error: {str(exc)[:80]}")
            if stale is not None:
                return stale
            return ToolResult(ok=False, display="fetch failed", error=str(exc)[:200])
        final = str(resp.url)
        body = resp.text
        if _is_challenged(resp.status_code, body):
            stale = await _stale_fallback(ctx, "fetch", url,
                                          f"{_domain(final)} answered with a bot wall")
            if stale is not None:
                return stale
            # Same honesty rule as search: a bot wall is not an empty page.
            return ToolResult(
                ok=False, display=f"{_domain(final)} blocked the fetch",
                error=(f"fetch_challenged: {_domain(final)} returned HTTP {resp.status_code} behind a "
                       "bot wall. The page was NOT read — do not treat this as empty content."))
        is_html = "html" in resp.headers.get("content-type", "").lower()
        content = (extract_main(body, final) if is_html else body)[:_MAX_TEXT]
        links = extract_links(body, final) if is_html else []
        payload = {
            "url": final, "domain": _domain(final), "title": title_of(body),
            "retrieved_at": datetime.now(UTC).isoformat(), "status": resp.status_code,
            "content": content, "links": links,
        }
        if resp.is_success:
            await _cache_put(ctx, "fetch", url, payload, resp.status_code)
        return ToolResult(
            ok=resp.is_success,
            output=payload,
            display=(f"fetched {_domain(final)} ({resp.status_code}) — "
                     f"{len(content)} chars, {len(links)} links"),
            error=None if resp.is_success else f"HTTP {resp.status_code}",
        )


# Cache TTLs. Background learning revisits the same topics for days, and without this every repeat is a
# real network round-trip (measured: 5 identical queries, ~1.5s each, all hitting upstream) — slow, and
# the single biggest way an unattended learner gets this host banned again.
_CACHE_TTL = {"search": timedelta(hours=24), "fetch": timedelta(days=7)}


def _cache_key(kind: str, target: str) -> str:
    return hashlib.sha256(f"{kind}:{target}".encode()).hexdigest()


async def _cache_get(ctx: ToolContext, kind: str, target: str,
                     *, stale_ok: bool = False) -> dict[str, Any] | None:
    """Replay a previous identical request. Silent no-op without a pool (a pool-less ToolContext is a
    legitimate context, e.g. a local one-shot) — caching must never be a precondition for working.

    ``stale_ok`` ignores the TTL and returns whatever is stored, annotated with its true age. It is
    used on the FAILURE paths only: a fresh hit is still a fresh hit, but when nothing can be reached
    the honest answer is "here is what I read nine days ago", not "search is unavailable". The age
    travels with the payload so the caller can say so out loud."""
    pool = getattr(ctx, "pool", None)
    if pool is None:
        return None
    with contextlib.suppress(Exception):
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT payload, fetched_at FROM sali.web_cache WHERE key_hash = $1",
                _cache_key(kind, target))
        if row is None:
            return None
        age = datetime.now(UTC) - row["fetched_at"]
        if not stale_ok and age >= _CACHE_TTL[kind]:
            return None
        payload = row["payload"]
        if not isinstance(payload, dict):
            return None
        out = dict(payload)
        out["cached_age_seconds"] = int(age.total_seconds())
        return out
    return None


def _describe_age(seconds: int) -> str:
    if seconds < 3600:
        return f"{max(1, seconds // 60)} minutes ago"
    if seconds < 86400:
        return f"{seconds // 3600} hours ago"
    return f"{seconds // 86400} days ago"


async def _stale_fallback(ctx: ToolContext, kind: str, target: str, reason: str) -> ToolResult | None:
    """The offline answer: serve what was stored, clearly stamped as old.

    Returns None when nothing was ever stored — and then the caller's honest failure stands. This
    never invents currency: `stale` is in the payload, the age is in the payload, the age is in the
    display line, and `retrieved_at` remains the ORIGINAL retrieval time, so nothing downstream can
    mistake this for a live read."""
    cached = await _cache_get(ctx, kind, target, stale_ok=True)
    if cached is None:
        return None
    age = _describe_age(int(cached.get("cached_age_seconds") or 0))
    cached["stale"] = True
    cached["cached"] = True
    cached["offline_note"] = (
        f"Served from Sali's local copy, retrieved {age}. The network could not be reached now "
        f"({reason}). Treat this as what was true then, not as a live check.")
    what = f"{len(cached.get('results') or [])} result(s)" if kind == "search" else "page"
    return ToolResult(ok=True, output=cached,
                      display=f"offline — {what} from local copy ({age})")


async def _cache_put(ctx: ToolContext, kind: str, target: str,
                     payload: dict[str, Any], status: int | None = None) -> None:
    pool = getattr(ctx, "pool", None)
    if pool is None:
        return
    with contextlib.suppress(Exception):
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO sali.web_cache (key_hash, kind, target, payload, status) "
                "VALUES ($1,$2,$3,$4,$5) ON CONFLICT (key_hash) DO UPDATE "
                "SET payload = EXCLUDED.payload, status = EXCLUDED.status, fetched_at = now()",
                _cache_key(kind, target), kind, target[:2000], payload, status)


_SO_QUESTION = re.compile(r"https?://(?:[a-z0-9-]+\.)?stackoverflow\.com/questions/(\d+)", re.IGNORECASE)


async def _fetch_stackoverflow(client: Any, url: str) -> dict[str, Any] | None:
    """Read a StackOverflow question through the open API instead of the walled page.

    stackoverflow.com answers a plain client with a "Just a moment..." challenge — but
    api.stackexchange.com is unwalled, and it returns the ANSWERS, which is the part that actually
    answers "how do I do this". Returns None when the url isn't a question, so the caller falls through
    to the ordinary path."""
    m = _SO_QUESTION.search(url or "")
    if m is None:
        return None
    qid = m.group(1)
    resp = await client.get(
        f"https://api.stackexchange.com/2.3/questions/{qid}/answers",
        params={"site": "stackoverflow", "order": "desc", "sort": "votes",
                "filter": "withbody", "pagesize": 3}, timeout=_TIMEOUT)
    if resp.status_code != 200:
        return None
    items = (resp.json() or {}).get("items") or []
    if not items:
        return None
    parts: list[str] = []
    for i, ans in enumerate(items, 1):
        body = html_to_text(str(ans.get("body") or ""))
        parts.append(f"## Answer {i} (score {ans.get('score', 0)}"
                     f"{', accepted' if ans.get('is_accepted') else ''})\n\n{body}")
    return {"content": "\n\n".join(parts), "title": f"StackOverflow answers for question {qid}"}


# Self-hosted SearXNG is the PRIMARY search backend. The public engines refuse this host outright:
# verified live, DuckDuckGo answers with HTTP 202 and a 14KB "anomaly" challenge page containing zero
# results. A local instance has no API key, no vendor, and no per-IP ban to hit.
_SEARXNG_URL = os.environ.get("SALI_SEARXNG_URL", "http://127.0.0.1:8888").rstrip("/")
_CHALLENGE = re.compile(r"anomaly|challenge|just a moment|attention required|captcha", re.IGNORECASE)


def _is_challenged(status: int, body: str) -> bool:
    """A bot-wall, NOT an empty result set.

    This distinction is the whole point. DuckDuckGo answers a blocked query with HTTP 202 — which
    httpx counts as success — so the old code parsed zero results and reported the GUESS "no results
    (search may be rate-limited)". Nothing downstream could tell "the web has nothing on this" from
    "I am banned", so the background research pass treated a permanent block as an empty topic,
    skipped the item, and logged nothing. Sali could sit blocked for weeks, silently learning nothing.
    """
    if status in (202, 403, 429):
        return True
    return bool(_CHALLENGE.search(body[:4000]))


async def _search_searxng(client: Any, query: str, limit: int) -> list[dict[str, Any]] | None:
    """Query the local SearXNG. Returns a (possibly empty) result list, or None if it is unreachable
    — the caller needs that difference to report an honest error."""
    resp = await client.get(f"{_SEARXNG_URL}/search",
                            params={"q": query, "format": "json"}, timeout=_TIMEOUT)
    if resp.status_code != 200:
        return None
    payload = resp.json()
    out: list[dict[str, Any]] = []
    for item in (payload.get("results") or []):
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        out.append({
            "title": unescape(str(item.get("title") or ""))[:200],
            "url": url,
            "domain": urlparse(url).netloc,
            "snippet": unescape(str(item.get("content") or ""))[:400],
        })
        if len(out) >= limit:
            break
    return out


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
        cached = await _cache_get(ctx, "search", f"{query}|{limit}")
        if cached is not None:
            return ToolResult(ok=True, output={**cached, "cached": True},
                              display=f"{len(cached.get('results') or [])} result(s) for {query!r} (cached)")
        tried: list[str] = []
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=_TIMEOUT, headers={"User-Agent": _UA}
        ) as client:
            # 1. Self-hosted SearXNG — the working backend on this host.
            searx: list[dict[str, Any]] | None = None
            with contextlib.suppress(Exception):
                searx = await _search_searxng(client, query, limit)
            if searx:
                payload = {"query": query, "retrieved_at": datetime.now(UTC).isoformat(),
                           "backend": "searxng", "results": searx}
                await _cache_put(ctx, "search", f"{query}|{limit}", payload, 200)
                return ToolResult(ok=True, output=payload,
                                  display=f"{len(searx)} result(s) for {query!r}")
            tried.append("searxng: no results" if searx is not None else "searxng: unreachable")

            # 2. DuckDuckGo HTML — last resort; usually walled for this host.
            try:
                resp = await client.post("https://html.duckduckgo.com/html/", data={"q": query})
            except httpx.HTTPError as exc:
                stale = await _stale_fallback(ctx, "search", f"{query}|{limit}",
                                              f"network error: {str(exc)[:80]}")
                if stale is not None:
                    return stale
                return ToolResult(ok=False, display="search failed",
                                  error=f"all search backends failed ({'; '.join(tried)}; "
                                        f"duckduckgo: {str(exc)[:120]})")
            if _is_challenged(resp.status_code, resp.text):
                stale = await _stale_fallback(ctx, "search", f"{query}|{limit}",
                                              "every search backend is blocked or unreachable")
                if stale is not None:
                    return stale
                # Say BLOCKED, never "no results" — see _is_challenged.
                return ToolResult(
                    ok=False, display="search backend blocked",
                    error=("search_backend_challenged: no search backend answered. "
                           f"{'; '.join(tried)}; duckduckgo returned HTTP {resp.status_code} "
                           "behind a bot wall. This is NOT an empty result set — searching is "
                           "unavailable until a backend (e.g. the local SearXNG) is reachable."))
            results = parse_ddg(resp.text)[:limit]
            payload = {"query": query, "retrieved_at": datetime.now(UTC).isoformat(),
                       "backend": "duckduckgo", "results": results}
            # This path never wrote to the cache, so anything found via the fallback backend was
            # lost the moment the turn ended — unreadable later, and unavailable offline.
            if results:
                await _cache_put(ctx, "search", f"{query}|{limit}", payload, resp.status_code)
            return ToolResult(
                ok=bool(results),
                output=payload,
                display=f"{len(results)} result(s) for {query!r}",
                error=None if results else f"no results for this query ({'; '.join(tried)})",
            )


def register_builtins(registry: ToolRegistry) -> None:
    registry.register(WebSearch())
    registry.register(WebFetch())
