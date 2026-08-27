"""Environmental timeline — "what happened today?" (spec §62).

Sali's durable `event` log already records the meaningful moments — what changed on the machine, what
it flagged, what it learned, when it talked with Almir. This turns that log into a readable timeline so
Sali can answer "what happened today?" from real history instead of reconstructing it. Read-only; the
renderer is deterministic (no model) and surfaces only meaningful events, not internal bookkeeping.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sali.core.enums import Capability, RiskLevel
from sali.tools.base import Tool, ToolResult
from sali.tools.context import ToolContext

# The event types worth putting on a human timeline (everything else is internal bookkeeping).
_MEANINGFUL = [
    "conversation.turn", "sali.proactive", "desktop.observed",
    "twin.entity_added", "twin.entity_removed", "learning.procedure", "learning.episode",
]


def summarize(event_type: str, payload: dict[str, Any]) -> str | None:
    """A short human phrase for one event, or None to omit it from the timeline."""
    if event_type == "conversation.turn":
        return "talked with Almir"
    if event_type == "sali.proactive":
        return f"flagged: {payload.get('message', '')}".strip()
    if event_type == "desktop.observed":
        if payload.get("tier") in ("important", "critical"):
            return str(payload.get("summary") or "").strip() or None
        return None
    if event_type == "twin.entity_added":
        return f"noticed new: {payload.get('key', '')}".strip()
    if event_type == "twin.entity_removed":
        return f"noticed gone: {payload.get('key', '')}".strip()
    if event_type == "learning.procedure":
        return "learned a procedure"
    if event_type == "learning.episode":
        return "folded recent activity into a memory"
    return None


def build_entries(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Render event rows into timeline entries, collapsing immediate repeats of the same summary."""
    entries: list[dict[str, str]] = []
    for r in rows:
        summary = summarize(str(r["event_type"]), r["payload"] or {})
        if not summary:
            continue
        at: datetime = r["created_at"]
        if entries and entries[-1]["summary"] == summary:
            continue  # collapse a run of the same thing
        entries.append({"at": at.strftime("%H:%M"), "summary": summary})
    return entries


class Timeline(Tool):
    name = "timeline"
    description = (
        "See what happened on the machine over a recent window — files changed, services that failed, "
        "things you flagged to Almir, what you learned, when you talked. Use it to answer 'what "
        "happened today?' or 'what changed recently?'. Returns a time-ordered list of meaningful events."
    )
    parameters = {
        "type": "object",
        "properties": {"hours": {"type": "number", "description": "How far back to look (default 24)."}},
    }
    risk_level = RiskLevel.R0
    capabilities = frozenset({Capability.READ})
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.pool is None:
            return ToolResult(ok=False, display="no datastore", error="the event log isn't available")
        hours = float(args.get("hours") or 24)
        hours = max(0.1, min(hours, 24 * 30))  # bound the window
        async with ctx.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT event_type, payload, created_at FROM event "
                "WHERE created_at > now() - make_interval(hours => $1) AND event_type = ANY($2) "
                "ORDER BY seq LIMIT 500",
                hours, _MEANINGFUL)
        entries = build_entries([dict(r) for r in rows])
        display = f"{len(entries)} events in the last {hours:g}h" if entries else "nothing notable"
        return ToolResult(ok=True, output={"entries": entries, "hours": hours}, display=display)


def register_builtins(registry: Any) -> None:
    registry.register(Timeline())
