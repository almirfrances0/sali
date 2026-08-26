"""ContextEngine — assemble a budgeted prompt from a retrieval bundle.

Builds sections (identity + security are P0 and never cut; live-note and tools are P1;
memories and graph facts P2; recent activity P3), packs them to the token budget, and renders
the survivors into a system message plus the user turn. Stale memories are surfaced as things
to verify, never asserted as current (rule 9). Token counts come from the provider with a
conservative margin so the budget is an upper bound, not a guess (fix H7).
"""

from __future__ import annotations

from dataclasses import dataclass

from sali.context.budget import Priority, Section, pack
from sali.provider.base import ChatMessage, ModelProvider, ToolSpec
from sali.retrieval.models import RetrievalBundle

IDENTITY = (
    "You are Sali, Almir's local AI running privately on his Kali Linux desktop. You are not "
    "the underlying model — you are the whole system around it. You are direct and grounded in "
    "evidence: you never invent facts and you say when you are unsure."
)
SECURITY_NOTE = (
    "You act within a permission policy: read-only tools run freely, changes to the system or "
    "network need confirmation, and destructive actions are refused. Never claim you did "
    "something you did not verify."
)
LIVE_NOTE = (
    "The user is asking about the machine's CURRENT state. Call a tool to inspect it now — do "
    "not answer from memory or guess."
)


@dataclass(slots=True)
class AssembledContext:
    messages: list[ChatMessage]
    est_tokens: int
    included: list[str]
    dropped: list[str]
    conflicts: list[str]


class ContextEngine:
    def __init__(
        self,
        provider: ModelProvider,
        *,
        ctx_tokens: int,
        reserve: int = 1024,
        identity: str = IDENTITY,
    ) -> None:
        self.provider = provider
        self.budget = max(256, ctx_tokens - reserve)
        self.identity = identity

    def _count(self, text: str) -> int:
        # Conservative upper bound: a heuristic tokenizer under-counts, so pad it (fix H7).
        return int(self.provider.count_tokens(text) * 1.2) + 4

    def assemble(
        self,
        query: str,
        bundle: RetrievalBundle,
        tool_specs: list[ToolSpec],
        *,
        live_note: str | None = None,
        history: list[tuple[str, str]] | None = None,
    ) -> AssembledContext:
        conflicts: list[str] = []
        sections: list[Section] = [
            Section("identity", Priority.P0, self.identity, self._count(self.identity)),
            Section("security", Priority.P0, SECURITY_NOTE, self._count(SECURITY_NOTE)),
        ]

        if history:
            convo = "Conversation so far:\n" + "\n".join(
                f"{role}: {content}" for role, content in history[-6:]
            )
            sections.append(Section("conversation", Priority.P1, convo, self._count(convo)))
        if live_note:
            sections.append(Section("live", Priority.P1, live_note, self._count(live_note)))
        if tool_specs:
            tools_text = "Tools you can call: " + "; ".join(
                f"{s.name} — {s.description}" for s in tool_specs
            )
            sections.append(Section("tools", Priority.P1, tools_text, self._count(tools_text)))

        if bundle.memories:
            lines = []
            for hit in bundle.memories:
                tag = f"(confidence {hit.effective_confidence:.2f}"
                tag += ", STALE — verify)" if hit.stale else ")"
                lines.append(f"- {hit.memory.content} {tag}")
                if hit.stale:
                    conflicts.append(hit.memory.content)
            text = "What you remember (each with its confidence):\n" + "\n".join(lines)
            avg = sum(h.score for h in bundle.memories) / len(bundle.memories)
            sections.append(Section("memories", Priority.P2, text, self._count(text), score=avg))

        if bundle.graph_facts:
            lines = [f"- {f.src} --{f.rel}--> {f.dst}" for f in bundle.graph_facts]
            text = "Known relationships:\n" + "\n".join(lines)
            sections.append(Section("graph", Priority.P2, text, self._count(text), score=0.6))

        if bundle.recent:
            lines = [f"- {r.event_type} at {r.at:%Y-%m-%d %H:%M}" for r in bundle.recent]
            text = "Recent activity:\n" + "\n".join(lines)
            sections.append(Section("recent", Priority.P3, text, self._count(text), score=0.3))

        result = pack(sections, self.budget, mandatory_tokens=self._count(query))
        system_text = "\n\n".join(s.text for s in result.sections)
        if conflicts and not live_note:
            system_text += (
                "\n\nSome memories above are marked STALE — verify them with a tool before "
                "relying on them; do not assert them as current."
            )
        messages = [
            ChatMessage(role="system", content=system_text),
            ChatMessage(role="user", content=query),
        ]
        return AssembledContext(
            messages=messages,
            est_tokens=result.total_tokens,
            included=[s.name for s in result.sections],
            dropped=result.dropped,
            conflicts=conflicts,
        )
