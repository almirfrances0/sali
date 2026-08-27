"""The research faculty (spec §9) — close gap → web → understand → store.

The learning queue notices what Sali doesn't know (§45); this is the half that actually LEARNS it: for
a few pending gaps, search the web, have the warm model distill a concrete answer from the results, and
deposit it as a durable memory with provenance (source=EXTERNAL_SOURCE, needs_grounding, the URL and
when it was retrieved) — so next time the fact is recalled locally and the internet isn't re-queried.
Bounded by a Budget and gated by a connectivity check, so it never runs offline and never turns into
unbounded crawling (§46/§79). Activates the queue's resolve() read-half that was previously dead code.
"""

from __future__ import annotations

from typing import Any

from sali.core.enums import MemoryLayer, MemorySource
from sali.core.net import check_internet
from sali.learning import queue
from sali.memory import writer as memory_writer
from sali.obs.log import get_logger
from sali.provider.base import ChatMessage, ModelProvider

log = get_logger("sali.learning.research")

_DISTILL_SYS = (
    "You are Sali, researching a specific question from web search results. In 1-3 sentences give the "
    "concrete answer, grounded ONLY in the snippets provided — no speculation. If the snippets don't "
    "actually answer it, reply with exactly 'none'."
)


async def _default_search(query: str) -> list[dict[str, str]]:
    from sali.tools.builtins.web import WebSearch
    from sali.tools.context import local_context

    res = await WebSearch().run({"query": query}, local_context())
    results = res.output.get("results", []) if res.ok else []
    return list(results)


async def _distill(
    provider: ModelProvider, query: str, results: list[dict[str, str]]
) -> tuple[str, str]:
    snippets = "\n".join(f"- {r.get('snippet', '')} ({r.get('url', '')})" for r in results[:5])
    try:
        res = await provider.chat(
            [ChatMessage(role="system", content=_DISTILL_SYS),
             ChatMessage(role="user", content=f"Question: {query}\n\nResults:\n{snippets}")],
            options={"temperature": 0.2, "top_k": 40})
        answer = (res.content or "").strip()
    except Exception:  # noqa: BLE001 - a model hiccup just means "learned nothing this pass"
        return "", ""
    if not answer or answer.lower().strip(" .'\"") == "none":
        return "", ""
    return answer, (results[0].get("url", "") if results else "")


async def research_pass(
    pool: Any, provider: ModelProvider, *, budget: queue.Budget | None = None,
    search: Any = None, online: Any = None,
) -> int:
    """Research up to Budget pending gaps and store what's found. Returns the number learned. `search`
    and `online` are injectable for tests; default to a live web search + connectivity probe."""
    budget = budget or queue.Budget()
    online = online or check_internet
    if not await online():
        return 0  # offline — never claim to have researched (§53)
    search = search or _default_search
    async with pool.acquire() as conn:
        items = await queue.pending(conn, limit=budget.max_new_per_pass)
    if not items:
        return 0

    learned = 0
    for item in items:
        results = await search(item.subject)
        if not results:
            continue  # web unavailable / rate-limited — leave pending, try again next pass
        answer, url = await _distill(provider, item.subject, results)
        async with pool.acquire() as conn:
            if not answer:  # researched but nothing conclusive — resolve so it isn't retried forever
                await queue.resolve(conn, item.id, outcome="researched; nothing conclusive")
                continue
            await memory_writer.remember(
                conn, layer=MemoryLayer.SEMANTIC, content=answer,
                source=MemorySource.EXTERNAL_SOURCE, needs_grounding=True, importance=0.5,
                note=f"researched online: {url}",
                structured={"kind": "researched", "query": item.subject, "source_url": url})
            await queue.resolve(conn, item.id, outcome=answer[:200])
        learned += 1
    if learned:
        log.info("researched", learned=learned)
    return learned
