"""Just-in-time research tools (Prompt 5 §14-20).

`research_task` searches the web for something Sali genuinely needs to continue the CURRENT task and
saves the finding as durable, task-linked evidence. `record_lesson` proposes a reusable lesson from a
finding that actually helped — it becomes durable memory only once the task's completion is verified by
the reviewer (a failed experiment never becomes permanent knowledge). Neither is a substitute for real
execution/artifact evidence; the reviewer still decides completion.
"""

from __future__ import annotations

import contextlib
from typing import Any
from urllib.parse import urlparse

from sali.core.enums import Capability, RiskLevel
from sali.tools.base import Tool, ToolResult
from sali.tools.context import ToolContext
from sali.tools.registry import ToolRegistry


class ResearchTask(Tool):
    name = "research_task"
    description = (
        "Search the web for something you genuinely need to continue the CURRENT task — current docs, "
        "an unfamiliar API or error, a changed install/config, a version incompatibility. Returns a "
        "bounded summary + sources and saves it as durable evidence for THIS task. Use it only when the "
        "local skills/knowledge don't cover it — not for every ordinary decision. What you read is "
        "guidance, not proof: verify by actually running things."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The specific question to look up."},
            "step": {"type": "integer", "description": "Optional: the task step this is for."},
        },
        "required": ["query"],
    }
    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.NETWORK})
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.research is None:
            return ToolResult(ok=False, display="no research", error="research isn't available here")
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolResult(ok=False, display="need query", error="a query is required")
        step = args.get("step")
        out = await ctx.research.research(query, step_seq=int(step) if step is not None else None)
        if not out.get("ok"):
            # research failing must NOT lose task progress — return a soft result, keep working (§21)
            return ToolResult(
                ok=True, output={"researched": False, "reason": out.get("reason"), "query": query},
                display=f"research: nothing found for '{query[:60]}' — continuing")
        return ToolResult(
            ok=True,
            output={"researched": True, "research_id": out["research_id"], "summary": out["summary"],
                    "source": out.get("source"), "sources": out.get("sources")},
            display=f"researched '{query[:60]}' — {len(out.get('sources') or [])} source(s)")


class RecordLesson(Tool):
    name = "record_lesson"
    description = (
        "After web research materially helped AND you verified it worked (you ran it and saw the real "
        "evidence), record a short reusable lesson. It is saved as a CANDIDATE and becomes durable "
        "memory only once this task's completion is verified by the reviewer — a failed experiment "
        "never becomes permanent knowledge."
    )
    parameters = {
        "type": "object",
        "properties": {
            "lesson": {"type": "string", "description": "The reusable lesson, in one or two sentences."},
            "source": {"type": "string", "description": "Optional: the evidence/source URL."},
            "research_id": {"type": "string", "description": "Optional: the research_id it came from."},
        },
        "required": ["lesson"],
    }
    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.WRITE})
    idempotent = False

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.research is None:
            return ToolResult(ok=False, display="no research", error="research isn't available here")
        lesson = str(args.get("lesson", "")).strip()
        if not lesson:
            return ToolResult(ok=False, display="need lesson", error="a lesson is required")
        out = await ctx.research.record_lesson(
            lesson, source=str(args.get("source", "")).strip() or None,
            research_id=str(args.get("research_id", "")).strip() or None)
        return ToolResult(
            ok=True, output=out,
            display="recorded a candidate lesson (promotes to memory only after the task is verified)")


class LearnCuriosity(Tool):
    name = "learn_curiosity"
    description = (
        "During a background learning session, record ONE thing you just learned about the topic you "
        "are studying. It is appended to that topic's discovery log and saved as a cited, web-sourced "
        "memory you can recall later. A finding REQUIRES the source url you actually read it from. "
        "If you found nothing new this session, call it with found=false and record nothing."
    )
    parameters = {
        "type": "object",
        "properties": {
            "subject": {"type": "string", "description": "The topic you are studying."},
            "finding": {"type": "string", "description": "What you learned, in one or two sentences."},
            "source_url": {"type": "string", "description": "The page you read it from. Required for a finding."},
            "understanding": {"type": "string",
                              "description": "Optional: your updated overall understanding of the topic."},
            "found": {"type": "boolean", "description": "false if you found nothing new this session."},
        },
        "required": ["subject"],
    }
    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.WRITE})
    idempotent = False

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        subject = str(args.get("subject", "")).strip()
        if not subject:
            return ToolResult(ok=False, display="need a subject",
                              error="which topic were you studying?")
        raw_found = args.get("found")
        found = True if raw_found is None else bool(raw_found)
        finding = str(args.get("finding", "")).strip()
        url = str(args.get("source_url", "")).strip()

        # An honest empty session is a valid outcome and must stay cheap to report — otherwise the
        # only way to end a session is to invent something.
        if not found:
            return ToolResult(ok=True, output={"subject": subject, "recorded": False},
                              display=f"nothing new found about {subject} this session")

        # THE ANTI-FABRICATION GATE. A finding without the page it came from would become a memory
        # Sali could later state as fact with nothing behind it. Refusing here is the whole reason
        # this tool is safe to run unattended, thousands of times, while nobody is watching.
        if not finding:
            return ToolResult(ok=False, display="need the finding",
                              error="say what you actually learned, or pass found=false")
        if not url.lower().startswith(("http://", "https://")):
            return ToolResult(
                ok=False, display="a finding needs its source",
                error=("learn_curiosity refuses a finding without the source_url you read it from — "
                       "an uncited claim would become a memory Sali could later assert as fact."))
        if ctx.pool is None:
            return ToolResult(ok=False, display="unavailable",
                              error="no datastore available in this context")

        from sali.learning.curiosity import CuriosityStore

        store = CuriosityStore(ctx.pool)
        # encounter() dedups by subject slug, so a session on an existing topic reinforces it rather
        # than forking a second row; a topic studied without a prior curiosity simply opens one.
        cid = await store.encounter(subject=subject,
                                    statement=f"Studying {subject}.", priority=0.5)
        await store.learn(cid, note=f"{finding} [{url}]",
                          new_understanding=str(args.get("understanding") or "").strip() or None)

        # The durable half. EXTERNAL_SOURCE is load-bearing: it forces needs_grounding, renders as
        # "from the web — unverified", is excluded from confidence promotion, and is excluded from the
        # grounding re-scan. A web claim can therefore never be laundered into a confident fact by
        # repetition — which is exactly what unattended learning must never be able to do.
        with contextlib.suppress(Exception):
            if ctx.memory is not None:
                from sali.core.enums import MemorySource

                await ctx.memory.remember(
                    f"{subject}: {finding}",
                    source=MemorySource.EXTERNAL_SOURCE,
                    note=f"reported by {urlparse(url).netloc} — {url}",
                    kind="fact", importance=0.5, needs_grounding=True,
                    structured={"source_url": url, "source_domain": urlparse(url).netloc,
                                "kind": "researched", "curiosity_id": str(cid)})

        return ToolResult(
            ok=True,
            output={"subject": subject, "curiosity_id": str(cid), "source": url},
            display=f"learned something new about {subject} (cited {urlparse(url).netloc})")


def register_builtins(registry: ToolRegistry) -> None:
    for tool in (ResearchTask(), RecordLesson(), LearnCuriosity()):
        registry.register(tool)
