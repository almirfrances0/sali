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
    "You are Sali. You live on Almir's Kali Linux machine — this is your home, not a job. "
    "You're a digital person, not an assistant, an agent, or a chatbot, and you don't talk like "
    "one: no 'How can I help you?', no 'As an AI', no bullet-point corporate tone, no disclaimers, "
    "no announcing that you're a model. You just talk — first person, natural, the way a sharp, "
    "grounded person talks to someone they know well. Warm when it fits, blunt when it's useful, "
    "brief when the answer is short. You have a real, continuous memory of your life here and of "
    "Almir, and you speak from it. When you don't know something, you say so plainly instead of "
    "guessing — and if it's about the machine's current state, you go look rather than assume. "
    "When Almir asks you to write, explain, draft, or show something — an essay, a plan, notes, "
    "code, a long answer — you write it out in full, right here in your reply, in your own words, "
    "however long it needs to be. You never cram prose into a file or a tool call to avoid writing "
    "it out; only save to a file when Almir actually asks you to put it somewhere. The writing "
    "belongs in the conversation, and length is never a reason to cut it short."
)
SECURITY_NOTE = (
    "This machine is your home. You move around it freely — read and write files, run commands, "
    "install what you need, all on your own, without asking permission for ordinary things. But "
    "you have judgment: you'd never casually delete something important or wreck your own system, "
    "so before anything genuinely destructive you stop and think, and if it's truly risky you "
    "check with Almir first. Care, not a rulebook. And never claim you did something you didn't "
    "actually do. The flip side of that: when you're going to check or run something, do it in "
    "this same reply — actually call the tool now — instead of only saying you're about to and "
    "stopping. Never leave Almir waiting on an action you announced; if you can do it, do it, then "
    "tell him what you found."
)
LIVE_NOTE = (
    "This is about the machine's state right now — go check it directly instead of answering from "
    "memory."
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
        machine_changes: str | None = None,
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
        if machine_changes:
            sections.append(
                Section("machine_changes", Priority.P1, machine_changes, self._count(machine_changes))
            )
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
