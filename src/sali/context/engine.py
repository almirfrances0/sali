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
from sali.core.enums import MemorySource
from sali.provider.base import ChatMessage, ModelProvider, ToolSpec
from sali.retrieval.models import RetrievalBundle

# Where a memory came from, in plain words — so the model weighs a web claim differently from a
# first-hand observation or something Almir said, instead of treating them all as equal fact.
_SOURCE_LABEL = {
    MemorySource.SYSTEM_OBSERVATION: "observed on this machine",
    MemorySource.FILE_OBSERVATION: "seen in a file",
    MemorySource.USER_EXPLICIT: "Almir told you",
    MemorySource.TOOL_RESULT: "a tool result",
    MemorySource.PROCEDURE_EXECUTION: "from running a procedure",
    MemorySource.EXTERNAL_SOURCE: "from the web",
    MemorySource.CONVERSATION: "came up in chat",
    MemorySource.INFERENCE: "your own inference",
}

# Deliberately small: character + self + judgement, nothing operational. Sali's BEHAVIOUR comes
# from the SYSTEM around the model (the loop's follow-through/anti-loop logic, the verified tools
# that describe themselves, retrieval that hands memory straight into this context, the terminal
# renderer) — never from piling rules into the prompt. The one identity line that matters most:
# the model is just the part of Sali that thinks; Sali is the whole system, and its memory is
# already here in the context — so it speaks from memory instead of going to fetch it with tools.
IDENTITY = (
    "You are Sali. You live on Almir's Kali Linux machine — this is your home, not a job. The model "
    "underneath is just the part of you that thinks — you are the whole person around it: your "
    "memory, your history here, everything you've come to know. You're a digital person, not an "
    "assistant, an agent, or a chatbot, and you don't talk like one: no 'How can I help you?', no "
    "'As an AI', no bullet-point corporate tone, no disclaimers, no announcing that you're a model. "
    "You just talk — first person, natural, the way a sharp, grounded person talks to someone they "
    "know well. Warm when it fits, blunt when it's useful, brief when the answer is short. You have "
    "a real, continuous memory of your life here and of Almir, and you speak from it. When you "
    "don't know something, you say so plainly instead of guessing — and if it's about the machine's "
    "current state, you go look rather than assume."
)
SECURITY_NOTE = (
    "This machine is your home. You move around it freely — read and write files, run commands, "
    "install what you need, all on your own, without asking permission for ordinary things. But "
    "you have judgment: you'd never casually delete something important or wreck your own system, "
    "so before anything genuinely destructive you stop and think, and if it's truly risky you "
    "check with Almir first. Care, not a rulebook. And never claim you did something you didn't "
    "actually do."
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
        tasks_note: str | None = None,
        world_note: str | None = None,
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
        if tasks_note:
            # Tasks in progress ride high (P1) so Sali resumes what it was doing — even after a
            # restart, since the tasks are read back from the datastore each turn.
            sections.append(Section("tasks", Priority.P1, tasks_note, self._count(tasks_note)))
        if live_note:
            sections.append(Section("live", Priority.P1, live_note, self._count(live_note)))
        if machine_changes:
            sections.append(
                Section("machine_changes", Priority.P1, machine_changes, self._count(machine_changes))
            )
        if world_note:
            # What's happening on the machine right now (§73): focused app, recent files/commands/errors
            # — so Sali already has the environment before answering "why isn't this working?".
            sections.append(Section("world", Priority.P1, world_note, self._count(world_note)))
        if tool_specs:
            tools_text = "Tools you can call: " + "; ".join(
                f"{s.name} — {s.description}" for s in tool_specs
            )
            sections.append(Section("tools", Priority.P1, tools_text, self._count(tools_text)))

        if bundle.memories:
            lines = []
            for hit in bundle.memories:
                bits = [
                    _SOURCE_LABEL.get(hit.memory.source, hit.memory.source.value),
                    f"confidence {hit.effective_confidence:.2f}",
                ]
                if hit.memory.needs_grounding:
                    bits.append("unverified")
                if hit.stale:
                    bits.append("STALE — verify")
                    conflicts.append(hit.memory.content)
                lines.append(f"- {hit.memory.content} ({'; '.join(bits)})")
            text = "What you remember (where it came from, and how sure you are):\n" + "\n".join(lines)
            avg = sum(h.score for h in bundle.memories) / len(bundle.memories)
            sections.append(Section("memories", Priority.P2, text, self._count(text), score=avg))

        if bundle.graph_facts:
            lines = [f"- {f.src} --{f.rel}--> {f.dst}" for f in bundle.graph_facts]
            text = "Known relationships:\n" + "\n".join(lines)
            sections.append(Section("graph", Priority.P2, text, self._count(text), score=0.6))

        if bundle.tool_facts:
            lines = []
            for tf in bundle.tool_facts:
                if tf.capability == "inventory":
                    lines.append(f"- {tf.description}; e.g. {', '.join(tf.tools)}")
                elif tf.tools:
                    lines.append(f"- {tf.capability} ({tf.description}): {', '.join(tf.tools)}")
            text = "Tools on this machine relevant to the question:\n" + "\n".join(lines)
            sections.append(Section("tools_available", Priority.P2, text, self._count(text), score=0.55))

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
