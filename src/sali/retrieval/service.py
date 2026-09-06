"""RetrievalService — runs the retrievers a plan selects and returns a fused bundle.

Memory (semantic + lexical, relevance-led) always runs; graph traversal and recent-activity
run only when the router asks for them. Graph seeding is deterministic entity linking:
query tokens are matched against node names, and the neighbours of the matches become facts.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from typing import Any

from sali.core.clock import Clock, SystemClock
from sali.core.temporal import TemporalService
from sali.core.toolvocab import CAPABILITY_VOCAB
from sali.graph import traverse
from sali.memory.service import MemoryService
from sali.provider.base import ModelProvider
from sali.retrieval.models import GraphFact, RecentItem, RetrievalBundle, ToolFact
from sali.retrieval.router import RetrievalPlan
from sali.twin.capabilities import capability_slugs_in

_STOP = {
    "the", "are", "and", "for", "which", "what", "does", "did", "how", "why", "who", "your",
    "you", "this", "that", "with", "from", "about", "connected", "related", "projects",
}


def _is_doing_turn(query: str, plan: RetrievalPlan) -> bool:
    """Is this a turn where what Sali has done before actually matters?

    `plan.use_experience` alone was too narrow, and it was measured: the router marks it True for "can you
    make a backup script AGAIN" but False for "build me a 5-page tailwind site" and "write a python script
    to back up photos" — exactly the requests where knowing the procedure, or the capability, decides
    whether Sali reuses competence or starts from zero. Across 50 real turns the capability section
    reached the model 0 times. Coding/build work counts, and so does any turn the router calls a task.
    """
    from sali.retrieval.router import CODING_WORK_RE

    return bool(plan.use_experience or plan.intent == "task" or CODING_WORK_RE.search(query or ""))


async def _empty_list() -> list:
    """Awaitable []; lets `gather()` create a task for the empty branch of a plan choice without
    special-casing None everywhere the results are awaited."""
    return []


class RetrievalService:
    def __init__(self, pool: Any, provider: ModelProvider, clock: Clock | None = None,
                 owner_timezone: str | None = None) -> None:
        self.pool = pool
        self.memory = MemoryService(pool, provider, clock)
        self.clock = clock or SystemClock()
        # Built from the SAME clock, so retrieval and the rest of the turn can never disagree about
        # what "yesterday" was. The owner's zone matters here specifically: his calendar day and UTC's
        # differ for three hours every day, and "what did we discuss yesterday" must search HIS day.
        self.temporal = TemporalService(self.clock, owner_timezone=owner_timezone)

    async def _touch_safe(self, memory_ids: list) -> None:
        """Write-back that never crashes the read path. Fire-and-forgot from `gather()`."""
        with contextlib.suppress(Exception):
            await self.memory.touch_many(memory_ids)

    async def gather(
        self, query: str, plan: RetrievalPlan, *, k: int = 6, scope: str | None = None
    ) -> RetrievalBundle:
        # SIX INDEPENDENT DB READS, RUN IN PARALLEL.
        #
        # This ran serially and was the tail of p90 context-assembly time (2.65 s over the two-day
        # window before the change). Every one of these queries takes the query text plus the plan and
        # produces its own bundle — none reads another's result — so they can run concurrently against
        # the asyncpg pool (min=2, max=8). The pool is not oversubscribed by five concurrent reads:
        # a turn holds one write connection at most beside these.
        #
        # `touch_many` (the write-back that marks a recalled memory used) is deliberately NOT awaited
        # here. It has to run AFTER the memories are known, and blocking retrieval on a write-back is
        # exactly the wrong trade — the reinforcement is a nicety, the reply is not. Fired-and-forgot
        # with contextlib.suppress inside the coroutine, so a failure never surfaces.
        window = self.temporal.resolve(query) if plan.use_recent else None
        _memories_task = asyncio.create_task(
            self.memory.retrieve(query, k=k, scope=scope)
            if (plan.use_vector or plan.use_keyword)
            else _empty_list())
        _graph_task = asyncio.create_task(self._graph_facts(query, k))
        _recent_task = asyncio.create_task(
            self._recent(k, window=window) if plan.use_recent else _empty_list())
        _tool_task = asyncio.create_task(
            self._tool_facts(query, k) if plan.use_tools else _empty_list())
        _exp_task = asyncio.create_task(self._experience(query, plan, scope=scope))
        _cap_task = asyncio.create_task(self._capabilities(query, plan, scope=scope))
        memories = await _memories_task
        # Start the touch AS SOON AS memories are known, but await it at the end of the function so it
        # is guaranteed complete when `gather()` returns. That was a contract callers relied on
        # (test_gather_reinforces_recalled_memories, and any reader that then queries access_count).
        # The parallelism win survives: `touch` runs concurrently with graph/recent/tools/experience/
        # capabilities instead of serially before them, which is where the ~200 ms latency actually
        # came from.
        _touch_task = (asyncio.create_task(self._touch_safe([h.memory.id for h in memories]))
                       if memories else None)
        graph_facts = await _graph_task
        # WHEN HE NAMED A PERIOD, SEARCH THAT PERIOD.
        #
        # The router already noticed "yesterday" / "last month" / "when did" and set use_recent — and
        # then the only thing that consumed it fetched the NEWEST k events by seq, whatever period had
        # been asked about. So "what did we discuss last month" answered with today. Resolving the
        # phrase to an actual span turns that into a real query.
        #
        # Only ever applied when a concrete period was named: "when did we start this?" carries
        # temporal INTENT but no window, resolves to None, and is left completely alone. And when a
        # window is named but nothing falls inside it, the empty result stands — "I have nothing from
        # yesterday" is the honest answer, and showing today's memories instead is how invented history
        # gets made.
        # Only when the ROUTER already judged this temporally intentful. Resolving alone is not enough
        # of a signal: any phrase that happens to name a period would otherwise narrow what Sali can
        # remember, and a retrieval filter is a heavy consequence for an incidental word.
        if window is not None and memories:
            in_window = [h for h in memories
                         if h.memory.valid_from is not None and window.contains(h.memory.valid_from)]
            # AND IT MUST NEVER CAUSE AMNESIA. If nothing falls in the window, the unfiltered pool
            # stands: an empty memory block is indistinguishable from "you know nothing about this",
            # which is a worse and less honest failure than a slightly wide window. Every memory now
            # carries its own "when" into the prompt, so with the full set in front of him Sali can say
            # "nothing from yesterday" himself — from the dates, rather than from an absence.
            memories = in_window or memories
        recent = await _recent_task
        tool_facts = await _tool_task
        procedures, experiences = await _exp_task
        capabilities = await _cap_task
        # Don't double-surface: a procedure/incident that also matched the generic pool shows ONLY in
        # its dedicated section (avoids the prompt-bloat the review flagged).
        surfaced = {h.memory.id for h in procedures + experiences}
        memories = [h for h in memories if h.memory.id not in surfaced]
        # Await the touch. It has been running in parallel this whole time; this just guarantees the
        # writes have landed before any consumer reads back access_count.
        if _touch_task is not None:
            await _touch_task
        return RetrievalBundle(memories=memories, graph_facts=graph_facts, recent=recent,
                               tool_facts=tool_facts, procedures=procedures, experiences=experiences,
                               capabilities=capabilities)

    async def _capabilities(
        self, query: str, plan: RetrievalPlan, *, scope: str | None = None
    ) -> list[dict]:
        """What Sali already knows how to do that bears on this turn.

        Only on doing/planning turns, because that is when reusing competence matters and because the
        context tail is tight. Best-effort: capability discovery must never break a turn.
        """
        if not _is_doing_turn(query, plan):
            return []
        try:
            from sali.learning.capability import CapabilityStore

            store = CapabilityStore(self.pool)
            found = list(await store.discover_for_task(query, scope_ref=scope, limit=3) or [])
            # SAY HOW SURE, NOT JUST WHAT. `check_availability` distinguishes a capability verified this
            # week from one last confirmed months ago, and it had no caller — so a skill Sali proved once
            # and never since was offered to planning as though it still worked. Almir's day-100 case:
            # recognise that an old capability may be stale and re-verify rather than blindly repeating.
            for cap in found:
                with contextlib.suppress(Exception):
                    cap["availability"] = await store.check_availability(
                        name=cap["name"], scope=cap.get("scope") or "environment",
                        scope_ref=cap.get("scope_ref"))
        except Exception:  # noqa: BLE001 - never break retrieval over an optional signal
            return []
        return found

    async def _experience(
        self, query: str, plan: RetrievalPlan, *, scope: str | None
    ) -> tuple[list[Any], list[Any]]:
        """On a doing/fixing turn, pull how Sali handled this before (procedural) and what happened
        last time (episodic incidents/episodes) — layer-scoped so they're guaranteed to surface (§4/§6).

        Same widened gate as capabilities: a procedure Sali already proved is worth most on the turn he is
        asked to do the thing, not only when Almir thinks to say "again".
        """
        if not _is_doing_turn(query, plan):
            return [], []
        procedures = await self.memory.retrieve(query, k=3, scope=scope, layer="procedural")
        experiences = await self.memory.retrieve(query, k=3, scope=scope, layer="episodic")
        return procedures, experiences

    async def _tool_facts(self, query: str, k: int) -> list[ToolFact]:
        """Answer a tool question from the inventory + capability graph: the tools that provide each
        capability the query refers to, or — if none is named — a summary of what's installed."""
        from sali.twin import selection

        slugs = capability_slugs_in(query)
        facts: list[ToolFact] = []
        async with self.pool.acquire() as conn:
            for slug in slugs[:k]:
                # Rank by LEARNED reliability + proven-use + safety (not alphabetically), so the tool
                # experience Sali actually accumulated reaches the prompt (§3/§8 memory USED).
                scored = await selection.score_tools(conn, slug)
                if scored:
                    facts.append(ToolFact(capability=slug, description=CAPABILITY_VOCAB.get(slug, ""),
                                          tools=[s.name for s in scored[:12]]))
            if not facts:  # a general "what tools do I have?" — summarize the inventory honestly
                total = await conn.fetchval("SELECT count(*) FROM discovered_tool WHERE available")
                if total:
                    sample = await conn.fetch(
                        "SELECT t.name FROM discovered_tool t JOIN graph_edge e ON e.src_id=t.node_id "
                        "WHERE t.available AND e.rel_type='provides_capability' AND e.valid_until IS NULL "
                        "GROUP BY t.name ORDER BY t.name LIMIT 15")
                    known = [r["name"] for r in sample]
                    facts.append(ToolFact(
                        capability="inventory",
                        description=f"{total} tools installed; {len(known)}+ with known capabilities",
                        tools=known))
        return facts

    async def _graph_facts(self, query: str, k: int) -> list[GraphFact]:
        words = [
            w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,}", query)
            if w.lower() not in _STOP
        ]
        if not words:
            return []
        # Escape LIKE metacharacters so a token like "sali_demo" matches a literal underscore,
        # not ILIKE's single-char wildcard (which would over-match unrelated node names).
        def _esc(w: str) -> str:
            return w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        patterns = [f"%{_esc(w)}%" for w in words[:6]]
        facts: list[GraphFact] = []
        seen: set[tuple[str, str, str]] = set()

        def _add(src: str, rel: str, dst: str, confidence: float, hops: int) -> None:
            key = (src, rel, dst)
            if key in seen or src == dst:
                return
            seen.add(key)
            facts.append(GraphFact(src=src, rel=rel, dst=dst, confidence=confidence, hops=hops))

        async with self.pool.acquire() as conn:
            seeds = await conn.fetch(
                "SELECT id, name FROM graph_node WHERE valid_until IS NULL AND name ILIKE ANY($1) "
                "LIMIT 3",
                patterns,
            )
            # Walk TWO hops (§16): the direct relationships of the mentioned entity, then one step
            # further out — so "why is Project X failing?" reaches Project X → Docker → the container,
            # not just Project X's immediate neighbours. Frontier is bounded so it can't fan out.
            frontier: list[tuple[Any, str]] = []
            for seed in seeds:
                for hop in await traverse.neighbors(conn, seed["id"]):
                    _add(seed["name"], hop["rel_type"], hop["node"].name, hop["confidence"], 1)
                    frontier.append((hop["node"].id, hop["node"].name))
            for node_id, node_name in frontier[:8]:
                for hop in await traverse.neighbors(conn, node_id):
                    _add(node_name, hop["rel_type"], hop["node"].name, hop["confidence"], 2)

        # Graph-distance ranking (§38): closest first, then most-confident — an important-but-far fact
        # never crowds out a directly-relevant one.
        facts.sort(key=lambda f: (f.hops, -f.confidence))
        return facts[:k]

    async def _recent(self, k: int, *, window: Any = None) -> list[RecentItem]:
        """Recent activity — or activity from the period he actually named.

        `created_at` is indexed, so the windowed form is a range scan rather than a scan of history."""
        async with self.pool.acquire() as conn:
            if window is not None:
                rows = await conn.fetch(
                    "SELECT event_type, created_at FROM event "
                    "WHERE event_type NOT IN ('memory.observed') "
                    "  AND created_at >= $1 AND created_at < $2 "
                    "ORDER BY seq DESC LIMIT $3",
                    window.start, window.end, k,
                )
            else:
                rows = await conn.fetch(
                    "SELECT event_type, created_at FROM event "
                    "WHERE event_type NOT IN ('memory.observed') "
                    "ORDER BY seq DESC LIMIT $1",
                    k,
                )
        return [RecentItem(event_type=r["event_type"], at=r["created_at"]) for r in rows]
