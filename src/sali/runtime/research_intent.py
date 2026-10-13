"""Tell a RESEARCH/explanation request apart from a BUILD/project request (deterministic, no model).

Almir's rule: "when i give sali a task a just research not a website task … he supposed to write in the
chat and a file to download". Research is information work — its deliverable is knowledge, so it belongs
in the conversation (a summary in chat + a downloadable report), bounded to minutes. A BUILD request
(a website, an app, a script, a deployment) legitimately becomes a multi-step task with files on disk.

The failure this fixes: Sali turned "walk through practical ways to integrate GEA/GEO onto an e-commerce
site" into a 3-step background task with no chat output and no file — research vanished into task steps
Almir could not see. So `plan_task` consults this: a research objective must NOT become a task.

Precision-first: BUILD shapes OVERRIDE research shapes (a false "research" would wrongly refuse a real
build), so only a request that is clearly information/explanation work AND carries no build intent is
classified research. Everything ambiguous stays allowed as a task.
"""

from __future__ import annotations

import re

# Asking for knowledge / explanation / analysis — the deliverable is prose, not files.
_RESEARCH_RE = re.compile(
    r"\b(?:research|look\s+into|find\s+out|dig\s+into|investigate|explain|walk\s+(?:me\s+)?through|"
    r"tell\s+me\s+about|teach\s+me|learn\s+about|overview\s+of|summar(?:y|ise|ize)|compare|"
    r"pros\s+and\s+cons|best\s+practices|guide\s+to|ways\s+to|how\s+(?:do|does|to|can)\b|"
    r"what\s+(?:is|are|were)\b|why\s+(?:is|are|does|do)\b|report\s+on|write\s+(?:me\s+)?(?:a\s+)?"
    r"(?:report|summary|overview|brief|analysis)\b|give\s+me\s+(?:a\s+)?(?:rundown|breakdown|"
    r"report|summary|overview))\b",
    re.IGNORECASE,
)

# Asking to BUILD / change the world — the deliverable is files or a running system. A build VERB
# OVERRIDES the research shapes above (a request can read like a question yet still be a build: "how
# about you build…"). Deliberately VERBS ONLY — a build NOUN alone ("…for a chat app", "the best CMS
# for a blog") is context, not build intent, and must not wrongly refuse a genuine research question.
_BUILD_RE = re.compile(
    r"\b(?:build|rebuild|create|make\s+(?:me\s+)?(?:a|an|the)\b|scaffold|generate\s+(?:a|an|the)\b|"
    r"set\s*up|deploy|implement|develop|refactor|migrate|install|configure|wire\s+up|"
    r"add\s+(?:a\s+)?(?:feature|endpoint|route|page|button|form|screen|component)|"
    r"fix|debug|patch|write\s+(?:me\s+)?(?:a\s+)?(?:script|app|program|website|site|api|function|class|"
    r"component|page|test))\b",
    re.IGNORECASE,
)


# A research request that asks for DEPTH — a big multi-page report, not a quick answer. These get the
# sectioned build (outline → write each section → append to the file); a plain research question stays a
# single fast pass. So "pros and cons of X" is one paragraph, but "detailed 10-page report on X" grows a
# real document.
_DEPTH_RE = re.compile(
    r"\b(?:detailed|comprehensive|thorough|in[\s-]?depth|deep[\s-]?dive|extensive|exhaustive|"
    r"full\s+report|long\s+report|complete\s+guide|whitepaper|white\s+paper|research\s+paper|"
    r"\d+\s*pages?|multi[\s-]?page|write\s+(?:me\s+)?(?:a\s+)?(?:detailed|full|long|comprehensive|"
    r"thorough|big)\b|as\s+much\s+detail|everything\s+about|deep\s+research)\b",
    re.IGNORECASE,
)


def wants_depth(text: str) -> bool:
    """True when a research request asks for a big, multi-page report (→ sectioned build), rather than a
    quick answer (→ single pass)."""
    t = (text or "").strip()
    return bool(t) and len(t) <= 2000 and bool(_DEPTH_RE.search(t))


# An EXPLICIT request for research / a written report — the ONLY thing that produces a downloadable .md
# file. Almir: "not everything is a research until i say so — otherwise we normally chat, no research
# task." So a normal question ("what is X", "explain Y", "compare A vs B", "how does Z work") is just
# chat with NO file; only an explicit ask ("research X", "write me a report on Y", "deep dive on Z")
# earns a report file.
_RESEARCH_REQUEST_RE = re.compile(
    r"\b(?:"
    r"do\s+(?:some\s+|a\s+bit\s+of\s+|a\s+)?research"
    r"|research\s+(?:on|about|into|this|that|it|the|whether|how|why|what|if)\b"
    r"|(?:write|make|create|prepare|compile|put\s+together|generate|draft|build|do)\s+(?:me\s+)?"
    r"(?:a\s+|an\s+|the\s+)?(?:detailed\s+|full\s+|comprehensive\s+|long\s+|proper\s+|quick\s+)?"
    r"(?:report|write[\s-]?up|whitepaper|white\s+paper|research\s+paper|analysis|brief|dossier|study)\b"
    r"|deep[\s-]?dive|deep\s+research"
    r"|give\s+me\s+(?:a\s+)?(?:detailed\s+|full\s+)?(?:report|write[\s-]?up|analysis|breakdown|dossier)\b"
    r"|(?:look\s+into|investigate|study)\s+.{2,60}?\band\s+(?:write|report|document|summar)"
    r")\b",
    re.IGNORECASE,
)


def is_research_request(text: str) -> bool:
    """True ONLY for an EXPLICIT request to research / produce a written report — the sole trigger for a
    downloadable .md file. A normal question is NOT research (that stays plain chat, no file)."""
    t = (text or "").strip()
    return bool(t) and len(t) <= 2000 and bool(_RESEARCH_REQUEST_RE.search(t))


def is_research_objective(text: str) -> bool:
    """True when this objective is information/explanation work that should be answered in the chat (with
    a downloadable report), NOT turned into a build task. Conservative: any build signal → False."""
    t = (text or "").strip()
    if not t or len(t) > 2000:
        return False
    if _BUILD_RE.search(t):
        return False                 # a build/project request — a task is right
    return bool(_RESEARCH_RE.search(t))


__all__ = ["is_research_objective", "is_research_request", "wants_depth"]
