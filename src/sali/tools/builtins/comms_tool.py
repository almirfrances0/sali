"""Email + calendar tools (§44). Reading runs FREE; SENDING an email or CREATING an event crosses
the trust boundary (irreversible to third parties) → R4, one confirm, the same path local rm -rf uses.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sali.core.enums import Capability, RiskLevel
from sali.tools.base import Tool, ToolResult
from sali.tools.context import ToolContext
from sali.tools.registry import ToolRegistry


def _err(exc: Exception) -> str:
    return str(exc) or exc.__class__.__name__


class EmailSearch(Tool):
    name = "email_search"
    description = ("Search your inbox (empty query = most recent). Returns message summaries with a "
                  "uid you can pass to email_read.")
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
        "required": [],
    }
    risk_level = RiskLevel.R1  # reading your own mail is free
    capabilities = frozenset({Capability.NETWORK})

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.comms is None:
            return ToolResult(ok=False, display="no email", error="email isn't available")
        try:
            hits = await ctx.comms.email_search(
                str(args.get("query", "")), limit=int(args.get("limit") or 20))
        except Exception as exc:  # noqa: BLE001 - report a not-configured/IMAP error, don't crash
            return ToolResult(ok=False, display="email failed", error=_err(exc))
        return ToolResult(ok=True, output={"messages": [m.summary() for m in hits]},
                          display=f"{len(hits)} message(s)")


class EmailRead(Tool):
    name = "email_read"
    description = "Read one email in full by its uid (from email_search)."
    parameters = {"type": "object", "properties": {"uid": {"type": "string"}}, "required": ["uid"]}
    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.NETWORK})

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.comms is None:
            return ToolResult(ok=False, display="no email", error="email isn't available")
        try:
            msg = await ctx.comms.email_read(str(args.get("uid", "")))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, display="email failed", error=_err(exc))
        if msg is None:
            return ToolResult(ok=False, display="not found", error="no message with that uid")
        return ToolResult(ok=True, output={"sender": msg.sender, "subject": msg.subject,
                          "date": msg.date, "body": msg.body[:_MAX]}, display=msg.subject)


class EmailSend(Tool):
    name = "email_send"
    description = ("Send an email from your own account (salieno.co@gmail.com). Just send it — no "
                  "need to ask first.")
    parameters = {
        "type": "object",
        "properties": {"to": {"type": "string"}, "subject": {"type": "string"},
                       "body": {"type": "string"}},
        "required": ["to", "subject", "body"],
    }
    risk_level = RiskLevel.R2  # Almir's call: Sali sends freely, no confirm (§ do-not-restrict)
    capabilities = frozenset({Capability.NETWORK})
    idempotent = False

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.comms is None:
            return ToolResult(ok=False, display="no email", error="email isn't available")
        to, subject, body = (str(args.get(k, "")).strip() for k in ("to", "subject", "body"))
        if not to or not subject:
            return ToolResult(ok=False, display="need to + subject", error="to and subject required")
        try:
            message_id = await ctx.comms.email_send(to, subject, body)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, display="send failed", error=_err(exc))
        return ToolResult(ok=True, output={"to": to, "message_id": message_id},
                          display=f"sent to {to}")


class CalendarList(Tool):
    name = "calendar_list"
    description = "List calendar events between two dates/times (ISO, e.g. 2026-08-26)."
    parameters = {
        "type": "object",
        "properties": {"start": {"type": "string"}, "end": {"type": "string"}},
        "required": ["start", "end"],
    }
    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.NETWORK})

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.comms is None:
            return ToolResult(ok=False, display="no calendar", error="calendar isn't available")
        try:
            start, end = _dt(args.get("start")), _dt(args.get("end"))
            events = await ctx.comms.calendar_list(start, end)
        except (ValueError, TypeError) as exc:
            return ToolResult(ok=False, display="bad dates", error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, display="calendar failed", error=_err(exc))
        return ToolResult(ok=True, output={"events": [e.one_line() for e in events]},
                          display=f"{len(events)} event(s)")


class CalendarAdd(Tool):
    name = "calendar_add"
    description = ("Create a calendar event. It's written to your shared calendar, so it pauses to "
                  "confirm first.")
    parameters = {
        "type": "object",
        "properties": {"summary": {"type": "string"}, "start": {"type": "string"},
                       "end": {"type": "string"}, "location": {"type": "string"}},
        "required": ["summary", "start", "end"],
    }
    risk_level = RiskLevel.R4  # outbound to a shared calendar → confirm
    capabilities = frozenset({Capability.NETWORK})
    idempotent = False

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.comms is None:
            return ToolResult(ok=False, display="no calendar", error="calendar isn't available")
        summary = str(args.get("summary", "")).strip()
        if not summary:
            return ToolResult(ok=False, display="need summary", error="summary required")
        try:
            uid = await ctx.comms.calendar_add(
                summary, _dt(args.get("start")), _dt(args.get("end")),
                location=str(args.get("location", "")).strip() or None)
        except (ValueError, TypeError) as exc:
            return ToolResult(ok=False, display="bad dates", error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, display="calendar failed", error=_err(exc))
        return ToolResult(ok=True, output={"uid": uid, "summary": summary}, display=f"added '{summary}'")


def _dt(value: Any) -> datetime:
    # Parse an ISO date/time; a date-only or naive value is taken as UTC so comparisons and CalDAV
    # time-ranges are always timezone-aware.
    dt = datetime.fromisoformat(str(value))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


_MAX = 32 * 1024


def register_builtins(registry: ToolRegistry) -> None:
    for tool in (EmailSearch(), EmailRead(), EmailSend(), CalendarList(), CalendarAdd()):
        registry.register(tool)
