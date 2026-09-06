"""Cross-turn self-consistency — FLAG-ONLY (§ reliability, Phase 3 #5).

verify/response_claims is stateless across replies: Sali can say "the About page is at /about-us.html"
on one turn and "/about-us.html is a 404 — there's no such page" three turns later, and BOTH pass. This
detector is READ-ONLY and never rewrites a reply. It looks for a DISTINCTIVE token — a path, domain,
filename, or quoted term (reusing runtime/verbatim.distinctive_tokens, so common words are excluded) —
whose EXISTENCE Sali negates in the current reply while a PRIOR assistant reply asserted it: a blatant
polarity flip on the same concrete thing. It returns the conflicts for the caller to RECORD (an event +
a ledger row), so it can be measured before it is ever allowed to change wording. Precision-first: it
fires only on a distinctive token carried by an explicit negation, and the caller excludes an honest
"I can't" denial and any change a tool this turn actually justified.
"""

from __future__ import annotations

import re
from typing import Any

from sali.runtime.verbatim import _DICT, distinctive_tokens

# Existence NEGATION cues — Sali saying a thing is NOT there / not real / gone.
_NEG_RE = re.compile(
    r"\b(?:no such|there(?:'s| is| are|'re)?\s+no|does(?:n'?t| not)\s+exist|isn'?t\s+(?:there|real|a\s+real)"
    r"|not\s+found|couldn'?t\s+find|can'?t\s+find|do(?:n'?t| not)\s+have\s+(?:a|an|any)"
    r"|is\s+(?:a\s+)?404|returns?\s+(?:a\s+)?404|gives?\s+(?:a\s+)?404|no\s+longer\s+(?:exists|there|valid)"
    # a fabricating assistant's OWN retraction phrasings (adversarial probe found these missing)
    r"|is\s+not\s+(?:a\s+)?real|isn'?t\s+real|never\s+existed|made\s+(?:it|that|them)\s+up"
    r"|fabricated|imaginary|was\s+(?:fake|made\s+up)|don'?t\s+think\s+.{0,20}\s+exists?)\b",
    re.IGNORECASE)

# A LEGITIMATE succession — Sali retiring an old name and naming its replacement in the same breath — is
# not a self-contradiction. If the negating sentence carries one of these cues, it is a redirect, not a
# flip, and must not be flagged.
_SUPERSEDE_RE = re.compile(
    r"\b(?:moved\s+(?:it|them)?\s*to|renamed\s+(?:it|them)?\s*to|replaced\s+(?:it|them)?\s*(?:with|by)"
    r"|switched\s+(?:it\s+)?to|changed\s+(?:it\s+)?to|migrated\s+(?:it\s+)?to|is\s+now\s+at"
    r"|now\s+(?:it'?s|lives|at)|fixed\s+(?:it|that)|deleted\s+(?:it|that)\s+and|updated\s+(?:it\s+)?to)\b",
    re.IGNORECASE)

# An honest capability denial ("I can't do that") is not a factual flip — never a conflict.
_DENY_RE = re.compile(r"\b(?:i\s+can'?t|i\s+cannot|i'?m\s+not\s+able|i\s+don'?t\s+have\s+(?:the\s+)?(?:tool|ability))\b",
                      re.IGNORECASE)

# A negation that REPORTS a search / listing attempt is an OBSERVATION Sali just made ("I tried listing X
# earlier and found nothing", "when I ran ls the glob matched nothing") — NOT an ungrounded existence-claim
# flip. An observation legitimately supersedes a prior belief, so this is a correction, never a
# self-contradiction to flag. (Root FP: "I tried listing `…/sali-works/serverless*` earlier and [no matches]"
# flagged `sali-works` — the perennial workspace root — on every prior reply that mentioned it, when the
# container was affirmed and only the glob was empty.)
_REPORT_RE = re.compile(
    r"\b(?:tried\s+(?:to\s+)?(?:list|find|locate|search|open|read|access|run|ls|stat|cat|grep)"
    r"|listing|searching\s+for|looking\s+for|searched\s+for|grep(?:p?ed|ping)?\s+for"
    r"|when\s+i\s+(?:ran|listed|checked|looked|tried)|(?:no|zero)\s+matches?|matched\s+nothing"
    r"|came\s+back\s+empty|returned\s+(?:no|nothing|empty|zero))\b",
    re.IGNORECASE)

_SENT_SPLIT = re.compile(r"(?<=[.!?\n])\s+")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text or "") if s.strip()]


def _negated_tokens(text: str, pool: set[str]) -> dict[str, str]:
    """Distinctive tokens from `pool` that appear in an EXISTENCE-NEGATION sentence in `text`. Returns
    {token: the sentence}. A token counts as negated when the sentence carries a negation cue AND the
    token itself is in it — so "no such /about-us.html" flags /about-us.html, but a plain mention doesn't."""
    out: dict[str, str] = {}
    for s in _sentences(text):
        if (_DENY_RE.search(s) or _SUPERSEDE_RE.search(s) or _REPORT_RE.search(s)
                or not _NEG_RE.search(s)):
            continue  # a redirect ("moved it to /new") or a search-report is not a contradiction
        low = s.lower()
        for tok in pool:
            if tok not in low or tok in out:
                continue
            idx = low.find(tok)
            # A glob search ("serverless*") reports an empty MATCH, not a missing resource — skip it.
            if low[idx + len(tok): idx + len(tok) + 1] == "*":
                continue
            # A parent directory present only as the PREFIX of a longer path in the same sentence is not
            # the thing being negated (the container was listable); only the fuller path is absent.
            if any(other != tok and other.startswith(tok) and other in low for other in pool):
                continue
            out[tok] = s.strip()
    return out


def _asserted_tokens(text: str, pool: set[str]) -> dict[str, str]:
    """Distinctive tokens from `pool` asserted POSITIVELY in `text` — mentioned in a sentence that is NOT
    an existence-negation. Returns {token: the sentence}."""
    out: dict[str, str] = {}
    for s in _sentences(text):
        if _NEG_RE.search(s):
            continue  # a negating sentence is not a positive assertion
        low = s.lower()
        for tok in pool:
            if tok in low and tok not in out:
                out[tok] = s.strip()
    return out


def find_conflicts(current_reply: str, prior_replies: list[str]) -> list[dict[str, Any]]:
    """Blatant cross-turn existence flips. For each distinctive token whose existence the CURRENT reply
    negates while some PRIOR reply asserted it, return {token, now, earlier}. Empty when there is no
    flip. The pool of distinctive tokens is drawn from the whole exchange so only real, concrete things
    (paths/domains/filenames/quoted terms) are ever compared — never common words."""
    if not current_reply or not prior_replies:
        return []
    pool = distinctive_tokens(current_reply)
    for p in prior_replies:
        pool |= distinctive_tokens(p)
    # Keep only tokens that are a real, concrete SUBJECT. distinctive_tokens' bare-word branch filters
    # against the small _COMMON set only, so ordinary dictionary words (earlier/around/listing/desktop)
    # leak in and produce cross-turn "flips" on pure word reuse. A path/domain/hyphenated token is
    # distinctive by its structure; a plain word must be long AND absent from the 69k-word dictionary.
    pool = {t for t in pool
            if ("." in t or "/" in t or "-" in t) or (len(t) >= 6 and t not in _DICT)}
    if not pool:
        return []
    negated_now = _negated_tokens(current_reply, pool)
    if not negated_now:
        return []
    conflicts: list[dict[str, Any]] = []
    for tok, now_sentence in negated_now.items():
        for prior in prior_replies:
            asserted = _asserted_tokens(prior, {tok})
            if tok in asserted:
                conflicts.append({"token": tok, "now": now_sentence[:240],
                                  "earlier": asserted[tok][:240]})
                break
    return conflicts


__all__ = ["find_conflicts"]
