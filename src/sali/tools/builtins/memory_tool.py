"""The remember tool — Sali deliberately keeps something worth remembering (spec §10-12).

Learning from the web or a conversation shouldn't vanish when the turn ends. When Sali decides a
fact is worth keeping, it calls this — and the fact is stored with honest PROVENANCE: an online
finding is an EXTERNAL_SOURCE observation ("reported by <domain>"), something that came up with
Almir is CONVERSATION (the model curating what it heard — NOT ground truth; USER_EXPLICIT is
reserved for Almir's actual captured message), and Sali's own conclusion is INFERENCE (weaker).
This is explicit, never automatic (§11), so stale web knowledge can't silently become fact.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from sali.core.enums import Capability, MemorySource, RiskLevel
from sali.tools.base import Tool, ToolResult
from sali.tools.context import ToolContext
from sali.tools.registry import ToolRegistry

_SOURCE = {
    # A model-initiated remember is Sali curating what it heard, not Almir asserting a fact — so it
    # can never mint USER_EXPLICIT (that stays for Almir's actual captured message). It lands as
    # CONVERSATION (priority 30 < a web finding's 40), honouring "never treat an LLM reply as truth".
    "user": (MemorySource.CONVERSATION, 0.6),      # came up with Almir — not asserted ground truth
    "web": (MemorySource.EXTERNAL_SOURCE, 0.6),    # found online — provenance required
    "inference": (MemorySource.INFERENCE, 0.4),    # Sali's own conclusion — weaker
}


class RememberFact(Tool):
    name = "remember"
    description = (
        "Save a fact worth keeping so you recall it later. Set source: 'user' if it came up with "
        "Almir, 'web' if you found it online (pass the url for provenance), or 'inference' for your "
        "own conclusion. Online facts are stored as 'reported by <source>', not as settled truth."
    )
    parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The fact, in one clear sentence."},
            "source": {"type": "string", "enum": ["user", "web", "inference"]},
            "url": {"type": "string", "description": "Where it came from, if online (for provenance)."},
        },
        "required": ["content"],
    }
    risk_level = RiskLevel.R1  # benign: Sali saving to its own memory
    capabilities = frozenset({Capability.WRITE})
    idempotent = False

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        content = str(args.get("content", "")).strip()
        if not content:
            return ToolResult(ok=False, display="nothing to remember", error="content is required")
        if ctx.memory is None:
            return ToolResult(ok=False, display="no memory", error="memory isn't available right now")

        source_key = str(args.get("source") or "inference").lower()
        source, importance = _SOURCE.get(source_key, _SOURCE["inference"])
        url = str(args.get("url", "")).strip()
        note: str | None = None
        shown = ""
        # A web fact is reported-at-a-moment, not settled truth — flag it so it surfaces as
        # "unverified" until Sali checks it against reality (§11: online never auto-becomes fact).
        needs_grounding = source is MemorySource.EXTERNAL_SOURCE
        if source is MemorySource.EXTERNAL_SOURCE:
            domain = urlparse(url).netloc if url else ""
            note = f"reported by {domain} — {url}" if url else "reported online"  # full provenance
            shown = f" (from {domain or 'the web'})"  # clean display

        await ctx.memory.remember(content, source=source, note=note, importance=importance,
                                  needs_grounding=needs_grounding)
        return ToolResult(ok=True, output={"remembered": content, "source": source.value},
                          display=f"remembered{shown}")


def register_builtins(registry: ToolRegistry) -> None:
    registry.register(RememberFact())
