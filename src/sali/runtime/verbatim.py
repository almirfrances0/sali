"""Verbatim exact-string anchoring (§ character-fidelity).

The reasoning model is a 35B run at 2-bit (Q2_K); it reproduces common English fine but GARBLES rare
exact tokens — a user's domain, a proper noun, a path — because it RETYPES them from sub-word pieces
instead of copying. Measured on the live transcript: "tanzahost" became "tanzhost" in 17 tool calls
(whois / curl / web_search), sending the work to the wrong site and confabulating a wrong owner; the
workspace "sali-works" came out "soli-works". Neither is cosmetic — a corrupted domain fetches a
different entity.

This module reconciles a tool call's string arguments against the exact tokens the USER actually typed
(plus the canonical workspace name). A tool token that is a near-miss of a user token — small edit
distance, shared prefix, and distinctive (not an everyday word) — is corrected back to the user's
spelling BEFORE the tool runs. It is deliberately PRECISION-FIRST: a wrong correction is worse than a
missed one, so every guard errs toward leaving the token alone. Every correction is returned, never
applied silently, so the caller logs it.
"""

from __future__ import annotations

import re
from typing import Any

# A "token" for correction purposes: a run that can hold a domain/path/proper-noun, so dots and hyphens
# stay INSIDE it ("tanzahost.com", "sali-works" are single tokens). Delimiters between tokens (spaces,
# slashes, pipes, "://") are preserved by re.sub, so only the token text is ever rewritten.
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

# Minimum length for a token to even be considered — short tokens ("api", "www", "com") are ambiguous
# and collide by accident, so they are never corrected.
_MIN_LEN = 6

# Arg keys whose value is a PAYLOAD (a file body, code, freeform prose) — never rewrite inside these,
# and never rewrite any string longer than this, so a document's contents are never touched. Correction
# is for short operational args: a query, a url, a command, a path, a domain.
_PAYLOAD_KEYS = frozenset({
    "content", "body", "text", "code", "data", "patch", "diff", "message", "prompt", "source"})
_MAX_VALUE_LEN = 600  # a realistic curl/web command with a garbled domain can exceed 200 chars

# A compact set of everyday 6+ letter words that can sit within an edit or two of a rare anchor — the
# source token is skipped if it is one of these, so a real word is never mutated into a user's proper
# noun. (Precision backstop; the distance + shared-prefix guards already carry most of the weight.)
_COMMON = frozenset({
    "please", "should", "search", "create", "delete", "update", "listen", "server", "system", "config",
    "status", "python", "docker", "github", "output", "result", "folder", "readme", "record", "report",
    "backup", "kernel", "memory", "public", "master", "branch", "commit", "before", "current", "friend",
    "family", "number", "mobile", "domain", "detail", "verify", "master", "google", "amazon", "twitter",
    "connect", "account", "channel", "gorgeous", "beautiful", "yellow", "select", "insert"})


def _load_dict() -> frozenset[str]:
    """A set of real English words (>=5 chars) from the system wordlist, if present. Used as a strong
    precision guard: a token that IS a genuine word (dessert, advice, breathe, solidworks-no/-yes) is
    never 'corrected' into a user's near-neighbor. Absent wordlist → empty set (falls back to _COMMON)."""
    import os
    for p in ("/usr/share/dict/words", "/usr/share/dict/american-english", "/usr/share/dict/british-english"):
        try:
            if os.path.exists(p):
                with open(p, encoding="utf-8", errors="ignore") as fh:
                    return frozenset(
                        w for w in (ln.strip().lower() for ln in fh)
                        if len(w) >= 5 and w.isalpha())
        except Exception:  # noqa: BLE001
            pass
    return frozenset()


_DICT: frozenset[str] = _load_dict()


def _lev_within(a: str, b: str, max_d: int) -> int | None:
    """Levenshtein distance between a and b, or None if it exceeds max_d. Bounded/early-exit."""
    la, lb = len(a), len(b)
    if abs(la - lb) > max_d:
        return None
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        best = cur[0]
        ai = a[i - 1]
        for j in range(1, lb + 1):
            cost = 0 if ai == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            best = min(best, cur[j])
        if best > max_d:
            return None
        prev = cur
    return prev[lb] if prev[lb] <= max_d else None


def distinctive_tokens(text: str) -> set[str]:
    """The exact strings from a user message worth protecting: domains (and their registrable core),
    back-ticked / quoted terms, path basenames, and any rare word (>=6 chars, not everyday). Everyday
    words and short tokens are excluded so the anchor set is only things a corruption would damage."""
    out: set[str] = set()
    if not text:
        return out
    t = text[:4000]
    # domains + their core label (tanzahost.com -> also tanzahost)
    for m in re.finditer(r"\b([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9-]{2,})+)\b", t, re.I):
        dom = m.group(1)
        out.add(dom.lower())
        core = dom.split(".")[0]
        if len(core) >= _MIN_LEN:
            out.add(core.lower())
    # back-ticked / single- / double-quoted spans -> their individual tokens
    for m in re.finditer(r"[`\"']([^`\"']{2,80})[`\"']", t):
        for tok in _TOKEN_RE.findall(m.group(1)):
            if len(tok) >= _MIN_LEN:
                out.add(tok.lower())
    # path basenames
    for m in re.finditer(r"/([A-Za-z0-9._-]{2,})", t):
        base = m.group(1)
        if len(base) >= _MIN_LEN:
            out.add(base.lower())
    # bare rare words (>=6, not everyday, not purely numeric)
    for tok in _TOKEN_RE.findall(t):
        low = tok.lower()
        if len(tok) >= _MIN_LEN and low not in _COMMON and not tok.isdigit() and re.search(r"[A-Za-z]", tok):
            out.add(low)
    # NEGATIVE context: a token the user is REJECTING ("its tanzahost not tanzhost", "wrong: X",
    # "missing the a") is the corruption itself — drop it from the anchor set so it stays CORRECTABLE
    # toward the real spelling, instead of anchoring its own error.
    negatives: set[str] = set()
    for m in re.finditer(r"\b(?:not|isn'?t|wrong|instead\s+of|no,?)\s+(?:https?://)?(?:www\.)?"
                         r"([A-Za-z0-9][A-Za-z0-9._-]{4,})", t, re.I):
        bad = m.group(1).lower()
        negatives.add(bad)
        negatives.add(bad.split(".")[0])
    return out - negatives


def _best_anchor(low: str, anchors: set[str], typed: set[str]) -> str | None:
    """The closest anchor to a single lowercase label, or None. Guards: >=6 chars, not typed/common/
    already-an-anchor, shares the FIRST character (cheap filter that still allows a corruption in the
    first few chars, e.g. soli->sali), and within edit distance 2 (1 for short tokens)."""
    if len(low) < _MIN_LEN or low in typed or low in _COMMON or low in anchors or low in _DICT:
        return None
    best: tuple[int, str] | None = None
    for a in anchors:
        if len(a) < _MIN_LEN or a[0] != low[0]:
            continue
        max_d = 1 if min(len(a), len(low)) <= 7 else 2
        d = _lev_within(low, a, max_d)
        if d is not None and d > 0 and (best is None or d < best[0]):
            best = (d, a)
    return best[1] if best else None


def _correct_token(tok: str, anchors: set[str], typed: set[str]) -> str | None:
    """If `tok` is a near-miss corruption of some anchor, return the corrected token; else None. A
    dotted domain is corrected LABEL-BY-LABEL (so www.tanzhost.co.tz -> www.tanzahost.co.tz, matching
    the registrable core), which also leaves short labels like www/co/tz alone."""
    low = tok.lower()
    if len(tok) < _MIN_LEN or low in typed or low in anchors:
        return None
    if "." in low and re.search(r"[a-z]", low):
        labels = low.split(".")
        changed = False
        for i, lab in enumerate(labels):
            fx = _best_anchor(lab, anchors, typed)
            if fx and fx != lab:
                labels[i] = fx
                changed = True
        return ".".join(labels) if changed else None
    return _best_anchor(low, anchors, typed)


def _correct_string(value: str, anchors: set[str], typed: set[str],
                    workspace: str | None) -> tuple[str, list[dict[str, str]]]:
    corrections: list[dict[str, str]] = []

    def _repl(m: "re.Match[str]") -> str:
        tok = m.group(0)
        # The workspace name is a known CONSTANT, so its corruptions are fixed UNCONDITIONALLY — even if
        # the user typed the wrong form (copying Sali's typo back) — because there is only one right
        # spelling. Checked first so "soli-works" -> "sali-works" always wins.
        if workspace and "." not in tok and tok.lower() != workspace:
            _lw = tok.lower()
            # lev 1 ONLY (so "soli-works" is fixed but "solidworks", distance 2, is left alone), and the
            # same real-word / everyday-word guards the other paths use — the workspace branch used to
            # skip them and collapse SolidWorks into the constant.
            d = _lev_within(_lw, workspace, 1)
            if (d is not None and 0 < d and len(tok) >= _MIN_LEN and _lw[0] == workspace[0]
                    and _lw not in _DICT and _lw not in _COMMON):
                corrections.append({"from": tok, "to": workspace})
                return workspace
        fixed = _correct_token(tok, anchors, typed)
        if fixed is None or fixed == tok.lower():
            return tok
        corrections.append({"from": tok, "to": fixed})
        return fixed

    return _TOKEN_RE.sub(_repl, value), corrections


def anchor_args(args: Any, anchors: set[str] | None, *,
                workspace: str | None = None) -> tuple[Any, list[dict[str, str]]]:
    """Return (corrected_args, corrections). Walks a tool's argument structure and corrects near-miss
    corruptions of user tokens (and, unconditionally, the workspace name) inside SHORT string values
    only — never a file body / long payload. Written not to raise, but the caller should still guard."""
    if not anchors and not workspace:
        return args, []
    anchors = anchors or set()
    typed = set(anchors)  # the exact things the user typed — never "correct" one of these
    all_corr: list[dict[str, str]] = []

    def _walk(node: Any, key: str | None) -> Any:
        if isinstance(node, str):
            if key in _PAYLOAD_KEYS or len(node) > _MAX_VALUE_LEN:
                return node
            fixed, corr = _correct_string(node, anchors, typed, workspace)
            all_corr.extend(corr)
            return fixed
        if isinstance(node, dict):
            return {k: _walk(v, k if isinstance(k, str) else key) for k, v in node.items()}
        if isinstance(node, list):
            return [_walk(v, key) for v in node]
        return node

    fixed = _walk(args, None)
    return fixed, all_corr


__all__ = ["distinctive_tokens", "anchor_args"]
