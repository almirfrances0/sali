"""Explicit knowledge types + epistemic status (spec §3/§12).

A memory is not just text: it has an epistemic KIND that says how much weight it deserves — did Sali
DIRECTLY OBSERVE it, is it a stated FACT, an INFERENCE it drew, a low-confidence BELIEF, a learned
PROCEDURE, or a past EPISODE? This derives that kind DETERMINISTICALLY from the memory's provenance
(source), layer, grounding flag and confidence — the things the store already records — so the
distinction is mechanical, never a prompt. It lets Sali say "I observed this" vs "I inferred this" vs
"I believe this but I'm not certain" vs "I don't know" — the trust distinction the directive requires.
"""

from __future__ import annotations

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
