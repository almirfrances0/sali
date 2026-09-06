"""Chat carries SUMMARIES and REFERENCES, never raw dumps (Almir §).

Two deterministic helpers for the RESPOND path:

* strip_file_dumps — when a turn CREATED files, Sali tends to paste each file's whole content into the
  reply. On a real project that buries the chat in file listings ("imagine the big website project how
  many files will i have"). The files are already on disk and downloadable via the task artifacts, so
  the reply only needs to NAME them. This replaces the pasted contents with one compact reference.
* extractive_lead — a no-inference fallback summary (the lead of the findings, cut at a sentence
  boundary) for when the model summariser is unavailable, so research always gets a short chat form.
"""

from __future__ import annotations

import re
from pathlib import Path

# A fenced code block: ```lang\n … \n```
_FENCE_RE = re.compile(r"```[^\n]*\n.*?```", re.S)

# A block only counts as a "file dump" worth stripping if it is genuinely large — a small illustrative
# snippet (a few lines) is fine to keep in the chat.
_DUMP_MIN_LINES = 12
_DUMP_MIN_CHARS = 600


def strip_file_dumps(text: str, created_paths: list[str]) -> tuple[str, list[str]]:
    """If this turn created files AND the reply contains large fenced code blocks (the pasted file
    contents), remove those blocks and append ONE compact reference to the created files. Returns
    (clean_text, created_file_names). No-op (returns text unchanged, []) when no files were created or
    there is nothing large to strip — so an ordinary code-example turn is never touched."""
    if not text or not created_paths:
        return text, []
    big = [m for m in _FENCE_RE.finditer(text)
           if m.group(0).count("\n") >= _DUMP_MIN_LINES or len(m.group(0)) >= _DUMP_MIN_CHARS]
    if not big:
        return text, []
    out = text
    for m in reversed(big):                      # remove from the end so offsets stay valid
        out = out[:m.start()] + out[m.end():]
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    names = [Path(p).name for p in created_paths if p]
    shown = ", ".join(f"`{n}`" for n in names[:12])
    if len(names) > 12:
        shown += f", +{len(names) - 12} more"
    ref = (f"📄 Created {len(names)} file{'s' if len(names) != 1 else ''}: {shown} — they're on disk "
           "and downloadable; I didn't paste the contents here to keep the chat readable.")
    return ((out + "\n\n" + ref).strip() if out else ref), names


# Absolute paths on THIS machine that Almir — on his phone — can never act on. A reply that names
# `/home/almir/Desktop/sali-works/report.md` reads to him as "Sali sent a path, not a file". These are
# Sali's INTERNAL machine paths (home/root/Users/tmp/var/opt, or anything under sali-works/Desktop), so
# in a chat reply they are pure noise — replace each with just the filename it points at.
_LOCAL_PATH_RE = re.compile(
    r"`?(?P<path>(?:/(?:home|root|Users|tmp|var|opt|mnt|srv)/[^\s`)\]]+"
    r"|/[^\s`)\]]*?/(?:sali-works|Desktop|Downloads)/[^\s`)\]]+))`?"
)


def strip_local_paths(text: str) -> str:
    """Replace absolute on-machine paths in a chat reply with just the file/dir name — Almir is on his
    phone and a local path is never something he can open, so it only reads as "he sent me a path"."""
    if not text:
        return text

    def _repl(m: "re.Match[str]") -> str:
        name = m.group("path").rstrip("/").rsplit("/", 1)[-1] or m.group("path")
        return f"`{name}`"

    return _LOCAL_PATH_RE.sub(_repl, text)


# DETERMINISTIC file-send: the model is unreliable at calling send_file (it said "sent" without
# sending). When Almir CLEARLY asks to send a specific file/folder, the runtime sends it itself so
# "send me X" always delivers. Conservative — only fires on a clear send verb + a concrete target
# (an absolute path, or a filename with an extension that actually exists), never on a vague ask.
_SEND_VERB_RE = re.compile(r"\b(?:send|share|deliver|zip\s+and\s+send)\b", re.IGNORECASE)
_ZIP_HINT_RE = re.compile(r"\bzip\b|\bcompress\b|\barchive\b|\bfolder\b|\bproject\b|\bdirectory\b", re.IGNORECASE)
_ABS_PATH_RE_IN = re.compile(r"(?P<p>(?:/|~/)[\w./@+\-]{2,})")
_FILENAME_RE_IN = re.compile(r"\b(?P<f>[\w.\-]+\.[A-Za-z0-9]{1,8})\b")


def send_request_target(user_input: str, search_dirs: list[str]) -> tuple[str, bool] | None:
    """If Almir clearly asked to send a specific file/folder, return (absolute_path, zip_it), else None.
    Resolves an explicit absolute path, or a filename-with-extension found in one of search_dirs."""
    t = (user_input or "").strip()
    if not t or len(t) > 500 or not _SEND_VERB_RE.search(t):
        return None
    zip_it = bool(_ZIP_HINT_RE.search(t))
    m = _ABS_PATH_RE_IN.search(t)
    if m:
        p = Path(m.group("p")).expanduser()
        if p.exists():
            return (str(p), zip_it or p.is_dir())
    fm = _FILENAME_RE_IN.search(t)
    if fm:
        name = fm.group("f")
        for base in search_dirs:
            cand = Path(base).expanduser() / name
            if cand.exists():
                return (str(cand), zip_it)
    # A FOLDER named in the message ("zip the sali-works folder and send it") — folders have no
    # extension, so match any word that names an existing directory in one of the search dirs.
    if zip_it:
        _skip = {"the", "and", "send", "zip", "folder", "project", "directory", "me", "it", "to",
                 "file", "files", "compress", "archive", "please", "sali", "share", "deliver", "my"}
        for w in re.findall(r"\b[\w.\-]{2,40}\b", t):
            if w.lower() in _skip:
                continue
            for base in search_dirs:
                cand = Path(base).expanduser() / w
                if cand.is_dir():
                    return (str(cand), True)
    return None


# A VAGUE send request — a send verb with no concrete target ("send me that", "send it", "send the
# file you made"). The runtime resolves it to the LAST file discussed (created this turn, named in a
# recent message, or most-recently-touched in the workspace) — Almir: "send me that" should just work.
_VAGUE_SEND_RE = re.compile(
    r"\b(?:send|share|deliver)\b[^.\n]{0,32}"
    r"\b(?:that|it|this|the\s+(?:file|report|zip|archive|document|one|thing|folder)|those|them|"
    r"what\s+you\s+(?:made|created|wrote|built|generated|just\s+\w+)|the\s+last\s+(?:file|one))\b"
    r"|\bsend\s+(?:it|that|those|them)\b",
    re.IGNORECASE,
)


def is_vague_send(user_input: str) -> bool:
    """True for a clear send request that names no concrete target ('send me that', 'send it')."""
    t = (user_input or "").strip()
    return bool(t) and len(t) <= 300 and bool(_VAGUE_SEND_RE.search(t))


def file_ref_in_text(text: str, search_dirs: list[str]) -> str | None:
    """The first file actually referenced in `text` that exists — an absolute path, or a filename-with-
    extension found in one of search_dirs. Used to resolve 'send me that' to a file named earlier."""
    for m in _ABS_PATH_RE_IN.finditer(text or ""):
        p = Path(m.group("p")).expanduser()
        if p.is_file():
            return str(p)
    for fm in _FILENAME_RE_IN.finditer(text or ""):
        for base in search_dirs:
            cand = Path(base).expanduser() / fm.group("f")
            if cand.is_file():
                return str(cand)
    return None


def extractive_lead(text: str, max_chars: int = 700) -> str:
    """A deterministic fallback 'summary': the opening of the findings, trimmed to a sentence boundary.
    Used only when the model summariser fails — research still gets a short chat form."""
    t = (text or "").strip()
    if len(t) <= max_chars:
        return t
    cut = t[:max_chars]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind("\n"))
    if end > max_chars * 0.5:
        cut = cut[: end + 1]
    return cut.strip() + " …"


__all__ = ["strip_file_dumps", "extractive_lead", "strip_local_paths", "send_request_target",
           "is_vague_send", "file_ref_in_text"]
