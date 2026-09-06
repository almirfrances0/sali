"""Explicit knowledge types + epistemic status (spec §3/§12).

A memory is not just text: it has an epistemic KIND that says how much weight it deserves — did Sali
DIRECTLY OBSERVE it, is it a stated FACT, an INFERENCE it drew, a low-confidence BELIEF, a learned
PROCEDURE, or a past EPISODE? This derives that kind DETERMINISTICALLY from the memory's provenance
(source), layer, grounding flag and confidence — the things the store already records — so the
distinction is mechanical, never a prompt. It lets Sali say "I observed this" vs "I inferred this" vs
"I believe this but I'm not certain" vs "I don't know" — the trust distinction the directive requires.
"""

from __future__ import annotations

import re
from enum import StrEnum

from sali.core.enums import MemoryLayer, MemorySource, is_observation


class KnowledgeType(StrEnum):
    OBSERVATION = "observation"  # a direct look at reality (system/file observation)
    FACT = "fact"                # stated/known, adequately confident
    INFERENCE = "inference"      # derived from other facts (Sali's own conclusion)
    BELIEF = "belief"            # held but uncertain (needs grounding / low confidence)
    ASSUMPTION = "assumption"    # taken as true with NO evidence yet — the weakest inference (§7)
    HYPOTHESIS = "hypothesis"    # a candidate explanation being considered, not asserted (§7)
    PROCEDURE = "procedure"      # a learned how-to
    EPISODE = "episode"          # a remembered past experience
    UNKNOWN = "unknown"          # not known — the honest answer at a recall boundary that found nothing


_LOW_CONFIDENCE = 0.5

# CAN SALI SETTLE THIS BY LOOKING AT HIS OWN MACHINE?
#
# This lived privately in runtime/loop.py, where only the `remember` tool could reach it — so the
# memory WRITER, which every other path goes through, had no way to ask the question. The result was
# measurable: of 73 live memories, ZERO carried needs_grounding, while 52 of them contained plainly
# checkable content ("this machine", "installed", "/home/...", "running"). The background grounding
# faculty has been waking every five minutes to verify a belief, and finding an empty set every time.
#
# It sits here because it is an EPISTEMIC question, not a runtime one: it says what KIND of claim this
# is, which is exactly what the rest of this module is for.
_CHECKABLE_RE = re.compile(
    r"(/home/|/usr/|/etc/|/var/|~/|\b(installed|running|version|uptime|listening|configured)\b|"
    r"\bthis (machine|pc|computer|host|box|laptop|server)\b|\bon (this|the) (machine|box|host)\b|"
    r"\b(you|i|we) (use|run|have|installed)\b|\b(gpu|cpu|ram|vram|disk|port|service|daemon|container)\b|"
    # Naming the tool someone works with is the single most common checkable claim in this
    # conversation, and the original pattern caught none of it: "your preferred code editor is Neovim"
    # was stored, recalled and repeated for turns on end while `nvim` was not on the machine at all.
    r"\b(editor|browser|terminal|shell|ide|compiler|runtime|database)\b)",
    re.IGNORECASE,
)


def looks_checkable(content: str) -> bool:
    """Could Sali confirm or falsify this by inspecting the machine he lives on?

    Deliberately broad. A false positive costs one background look, which is cheap and settles the
    matter; a false negative means a belief he could have checked stays unchecked forever."""
    return bool(_CHECKABLE_RE.search(content or ""))

_STATUS: dict[KnowledgeType, str] = {
    KnowledgeType.OBSERVATION: "I directly observed this",
    KnowledgeType.FACT: "I know this",
    KnowledgeType.EPISODE: "I remember this from a past experience",
    KnowledgeType.PROCEDURE: "this is a procedure I've learned",
    KnowledgeType.INFERENCE: "I inferred this from other facts",
    KnowledgeType.BELIEF: "I believe this but I'm not fully certain",
    KnowledgeType.ASSUMPTION: "I'm assuming this — I have no evidence for it yet",
    KnowledgeType.HYPOTHESIS: "this is a hypothesis I'm considering, not something I've confirmed",
    KnowledgeType.UNKNOWN: "I don't know this",
}


def classify_knowledge(
    source: MemorySource, layer: MemoryLayer, *, needs_grounding: bool = False,
    confidence: float = 1.0, evidence_count: int | None = None,
) -> KnowledgeType:
    """The epistemic kind of a memory, from its provenance/layer/grounding/confidence. Deterministic.

    ``evidence_count`` is opt-in: when a caller reports it and a low-confidence INFERENCE has ZERO
    backing evidence, the memory is an ASSUMPTION (weaker than a BELIEF). Left None, behaviour is
    unchanged (a weak inference stays a BELIEF) — so this classifier is a superset, never a regression."""
    if layer is MemoryLayer.PROCEDURAL:
        return KnowledgeType.PROCEDURE
    if layer is MemoryLayer.EPISODIC:
        return KnowledgeType.EPISODE
    if is_observation(source):
        return KnowledgeType.OBSERVATION       # a direct inspection of reality outranks the rest
    weak = needs_grounding or confidence < _LOW_CONFIDENCE
    if source is MemorySource.INFERENCE and weak and evidence_count == 0:
        return KnowledgeType.ASSUMPTION         # an inference with no evidence at all — a bare guess
    if weak:
        return KnowledgeType.BELIEF             # held, but not yet trustworthy
    if source is MemorySource.INFERENCE:
        return KnowledgeType.INFERENCE
    return KnowledgeType.FACT


def epistemic_status(ktype: KnowledgeType, confidence: float = 1.0) -> str:
    """A short first-person statement of HOW Sali knows this — the honest framing for a recalled item."""
    base = _STATUS[ktype]
    _hedged = (KnowledgeType.BELIEF, KnowledgeType.ASSUMPTION, KnowledgeType.HYPOTHESIS, KnowledgeType.UNKNOWN)
    if ktype not in _hedged and confidence < _LOW_CONFIDENCE:
        return f"{base} (low confidence)"
    return base
