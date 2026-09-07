"""The research faculty (spec §9) — close gap → web → understand → store.

The learning queue notices what Sali doesn't know (§45); this is the half that actually LEARNS it: for
a few pending gaps, search the web, have the warm model distill a concrete answer from the results, and
deposit it as a durable memory with provenance (source=EXTERNAL_SOURCE, needs_grounding, the URL and
when it was retrieved) — so next time the fact is recalled locally and the internet isn't re-queried.
Bounded by a Budget and gated by a connectivity check, so it never runs offline and never turns into
unbounded crawling (§46/§79). Activates the queue's resolve() read-half that was previously dead code.
"""

from __future__ import annotations

import json
from typing import Any

from sali.core.enums import MemoryLayer, MemorySource
from sali.core.net import check_internet
from sali.learning import queue
from sali.memory import writer as memory_writer
from sali.obs.log import get_logger
from sali.provider.base import ChatMessage, ModelProvider
from sali.provider.presets import BALANCED

log = get_logger("sali.learning.research")

_DISTILL_SYS = (
    "You are Sali, researching a specific question from web search results. In 1-3 sentences give the "
    "concrete answer, grounded ONLY in the snippets provided — no speculation. If the snippets don't "
    "actually answer it, reply with exactly 'none'."
)


def _pool_search(pool: Any) -> Any:
    """A search bound to the DB pool. WITHOUT the pool the web tool's cache writes are silent no-ops,
    so everything Sali researched in the background was unreadable the moment the link went down —
    the exact opposite of "if there is no internet he can still use what he searched before"."""
    async def _search(query: str) -> list[dict[str, str]]:
        from sali.config.settings import Settings
        from sali.core.clock import SystemClock
        from sali.tools.builtins.web import WebSearch
        from sali.tools.context import ToolContext

        ctx = ToolContext(settings=Settings(), clock=SystemClock(), pool=pool)
        res = await WebSearch().run({"query": query}, ctx)
        results = res.output.get("results", []) if res.ok else []
        return list(results)

    return _search


async def _unblock_step(conn: Any, item: queue.LearningItem, answer: str) -> bool:
    """Hand a lesson back to the work it unblocks. Returns True if a step was actually reopened.

    Deliberately does NOT clear `attempts`, `last_error` or `failure_class`: the retry policy reads
    them to decide whether another attempt is even warranted, and the engine shows Sali "you already
    tried this N times, do not repeat that approach". Wiping them would turn a learned retry into an
    amnesiac one that walks straight back into the same wall.
    """
    if item.task_id is None or item.step_seq is None:
        return False
    # A lesson that does not actually unstick the step must not reopen it forever. Three reopenings
    # is enough to tell the difference between "Sali needed to look something up" and "this step
    # cannot be done here" — after that it stays failed and stays visible, which is the honest state.
    reopened = await conn.fetchval(
        "SELECT count(*) FROM event WHERE event_type = 'task.step_unblocked' "
        "  AND payload->>'task_id' = $1 AND (payload->>'step_seq')::int = $2",
        str(item.task_id), item.step_seq)
    if int(reopened or 0) >= 3:
        return False
    row = await conn.fetchrow(
        "UPDATE task_step s SET status='pending', verified=false, "
        "  note = left(coalesce(s.note || E'\n', '') || 'learned: ' || $3, 2000) "
        "FROM task t "
        "WHERE s.task_id = $1 AND s.seq = $2 AND s.status = 'failed' AND t.id = s.task_id "
        "  AND t.status NOT IN ('done','failed','abandoned','cancelled','superseded') AND t.archived_at IS NULL "
        # Same correction as the enqueue side: 'completed' is not a status here, so this admitted
        # done/failed/ABANDONED tasks. Reopening a step on work Almir abandoned is precisely the
        # dead-work resurrection this system has been bitten by, so the tombstone is checked too.
        "  AND NOT EXISTS (SELECT 1 FROM revoked_intent r "
        "                  WHERE r.task_id = t.id AND r.superseded_by IS NULL) "
        "RETURNING s.task_id, s.seq",
        item.task_id, item.step_seq, answer[:600])
    if row is None:
        return False  # step already moved on, or the task is over — the lesson still stands
    # Pass the DICT, not json.dumps(dict). This connection carries the asyncpg jsonb codec whose
    # encoder is itself json.dumps, so a pre-serialised string gets encoded TWICE and lands as a
    # jsonb string scalar — `payload->>'task_id'` then reads NULL and every consumer of this event
    # (the reopen bound below, the API, the app) silently sees nothing.
    await conn.execute(
        "INSERT INTO event (event_type, payload) VALUES ('task.step_unblocked', $1::jsonb)",
        {"task_id": str(item.task_id), "step_seq": item.step_seq,
         "subject": item.subject[:200], "learned": answer[:300]})
    return True


async def _distill(
    provider: ModelProvider, query: str, results: list[dict[str, str]]
) -> tuple[str, str]:
    snippets = "\n".join(f"- {r.get('snippet', '')} ({r.get('url', '')})" for r in results[:5])
    try:
        res = await provider.chat(
            [ChatMessage(role="system", content=_DISTILL_SYS),
             ChatMessage(role="user", content=f"Question: {query}\n\nResults:\n{snippets}")],
            # BALANCED explicitly: the provider's default preset is DETERMINISTIC, which pins
            # seed=42, and options merge OVER a preset rather than replacing it — so this call was
            # silently fixed-seed. A summariser with a frozen seed can't reconsider anything.
            options={"temperature": 0.2, "top_k": 40}, preset=BALANCED)
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
    search = search or _pool_search(pool)
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
            if await _unblock_step(conn, item, answer):
                log.info("step_unblocked", task_id=str(item.task_id), step=item.step_seq)
        learned += 1
    if learned:
        log.debug("researched", learned=learned)
    return learned
