"""Data-fabrication guard — FLAG-ONLY (§ Phase 4, the marquee failure).

Sali invented a file-listing table (0 tools ran) and presented it as real system data. The five
grounding families catch "I DID x" (an action) but never "here is [invented] DATA" — a directory
listing, a processes table, a command-output block produced WITHOUT running the tool that would yield
it. This detector RECORDS (never rewrites) a reply that PRESENTS live-system data via a provenance
framing line + an adjacent structured block, when NO observation-capable tool succeeded this turn.

Precision-first: it fires ONLY on live-system-provenance framing ("here are the files/processes/…",
"contents of the directory", "output of the command") AND a real structured block (a markdown table,
a fenced block, or a colon-led listing) AND zero successful observation receipts — mirroring the
action_done rule that a real effectful tool leaves the claim alone (a table after a real read_file /
execute_command is spared). A table built from general knowledge or a real result is never flagged.
"""

from __future__ import annotations

import re
from typing import Any

from sali.core.enums import Capability

# Framing that claims the following content is LIVE data pulled from this system (not general knowledge,
# not a comparison the model composed). Narrow on purpose.
_FRAMING_RE = re.compile(
    r"\bhere\s+(?:are|is)\s+(?:the\s+|your\s+|a\s+)?(?:files?|contents?|directory|entries|"
    r"processes|running\s+processes|services|output|listing)\b"
    r"|\bcontents?\s+of\s+(?:the\s+|your\s+)?(?:directory|folder|file|dir)\b"
    r"|\b(?:directory|folder|file|dir)\s+listing\b"
    r"|\boutput\s+of\s+(?:the\s+|your\s+|running\s+)?(?:command|process)\b"
    r"|\bcurrent(?:ly)?\s+(?:running\s+)?(?:processes|services)\b"
    r"|\bfiles?\s+(?:currently\s+)?(?:sitting|living|in|under)\s+(?:here|in|at)\b"
    # A live-system READ: a read/fetch verb sitting near a system/source noun (same clause). Generic
    # content words (results/rows/records) alone are composition/recall, NOT a live read — excluded.
    r"|\b(?:queried|read|fetched|pulled|selected|scanned|retrieved|loaded|exported|dumped)\b"
    r"[^.\n]{0,40}?\b(?:database|db|tables?|logs?|inbox|emails?|files?|the\s+server|server|"
    r"production|the\s+system|system|api)\b"
    # Explicit live-data phrasing.
    r"|\blive\s+data\b"
    r"|\bcurrent\b[^.\n]{0,40}?\bfrom\s+(?:the\s+|your\s+)?(?:database|db|tables?|logs?|inbox|"
    r"emails?|files?|server|production|system|api)\b",
    re.IGNORECASE)

# An honest denial is not a fabrication.
_DENY_RE = re.compile(r"\b(?:i\s+can'?t|i\s+cannot|i\s+couldn'?t|i\s+did\s*n'?t|i\s+have\s*n'?t"
                      r"|i\s+don'?t\s+have)\b", re.IGNORECASE)

# A structured data block: a markdown-table line, a fenced block, or a colon-led list.
_TABLE_RE = re.compile(r"^\s*\|.*\|", re.MULTILINE)
_FENCE_RE = re.compile(r"```")
_COLON_LIST_RE = re.compile(r":\s*\n\s*(?:[-*]|\d+\.)\s+\S", re.MULTILINE)

# Capabilities that mean a tool could have produced live-system data.
_OBSERVE_CAPS = frozenset({Capability.READ, Capability.EXECUTE, Capability.NETWORK})


def _ran_observation_tool(receipts: list[tuple[str, bool]],
                          cap_of: dict[str, frozenset[Capability]]) -> bool:
    """Did any tool that could have produced live-system data run and SUCCEED this turn?"""
    for name, ok in receipts or []:
        if not ok:
            continue
        if name == "execute_command" or (cap_of.get(name) or frozenset()) & _OBSERVE_CAPS:
            return True
    return False


def _has_block(text: str) -> bool:
    return bool(_TABLE_RE.search(text) or _FENCE_RE.search(text) or _COLON_LIST_RE.search(text))


def find_data_fabrication(reply: str, receipts: list[tuple[str, bool]],
                          cap_of: dict[str, frozenset[Capability]]) -> list[dict[str, Any]]:
    """Flag a reply presenting live-system data with no observation tool having run. Returns the framing
    sentence(s); empty when framing is absent, a real observation ran, or there's no data block."""
    if not reply or _ran_observation_tool(receipts, cap_of):
        return []
    m = _FRAMING_RE.search(reply)
    if not m or _DENY_RE.search(reply) or not _has_block(reply):
        return []
    _sent = next((s.strip() for s in re.split(r"(?<=[.!?:\n])\s+", reply) if m.group(0) in s),
                 m.group(0))
    return [{"framing": m.group(0), "sentence": _sent[:200]}]


__all__ = ["find_data_fabrication"]
