"""Deterministic noise filter + importance score (sali3 §41: filter first, never LLM every event).

Pure functions — no I/O, no model — so the hot path stays cheap and every decision is testable. The
filter drops the overwhelming majority of events (build artifacts, caches, editor temp files); the
score ranks what survives so the engine can aggregate and surface only what matters.
"""

from __future__ import annotations

import os

from sali.events.base import DesktopEvent, EventKind

# Directory names whose contents are churn, not intent — anywhere in the path.
_NOISE_DIRS = frozenset({
    # Sali's OWN workspace. 344 of 421 observations (82%) were his own task files moving around, and
    # the single proactive desktop message he has ever sent Almir was "moved 5 files in
    # 2e7fd9e2-9c08-4019-911f-18983254bb81" — himself, narrating his own filing, as a UUID. Watching
    # yourself work is not perception.
    "sali-works",
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".cache",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "target", "dist", "build",
    ".next", ".gradle", ".idea", ".DS_Store", "site-packages", ".cargo", ".rustup",
})
# Suffixes that are transient/derived, never a meaningful edit.
_NOISE_SUFFIXES = (".pyc", ".pyo", ".swp", ".swx", ".swo", ".tmp", ".temp", ".part", ".crdownload",
                   ".lock", ".log", "~", ".orig", ".bak", ".o", ".class")
# Files a source edit tends to be — bumps the score.
_SOURCE_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".c", ".h", ".cpp", ".java",
                    ".rb", ".sh", ".sql", ".toml", ".yaml", ".yml", ".json", ".md", ".txt", ".html",
                    ".css", ".tf")


def is_noise(event: DesktopEvent) -> bool:
    """True if this event should be dropped before it ever costs anything downstream."""
    if event.kind is EventKind.WINDOW_FOCUS:
        return not event.target.strip()  # a focus with no app name is noise; real focuses are kept
    path = event.target
    parts = path.split(os.sep)
    name = parts[-1] if parts else path
    if any(seg in _NOISE_DIRS for seg in parts):
        return True
    if name.startswith(".#") or (name.startswith("#") and name.endswith("#")):  # emacs/lock temp
        return True
    return name.endswith(_NOISE_SUFFIXES)


def score(event: DesktopEvent) -> float:
    """Importance in [0, 1] — deterministic, from kind + path shape. Higher = more worth surfacing."""
    if event.kind is EventKind.WINDOW_FOCUS:
        # 0.6, and the exact value is the whole point. This returned 0.5 while attention's INTERESTING
        # tier begins at 0.55, so EVERY window focus was tiered `routine` -> `ignore` and dropped
        # before it was ever written: 421 desktop observations in the log, not one of them about what
        # Almir had on screen. The comment above claimed "always-relevant" while the number said
        # otherwise. Knowing which app he is in is the cheapest true thing Sali can know about him.
        #
        # RECORD, deliberately not NOTIFY: this makes him AWARE, it must never make him talk. Speaking
        # is still gated by attention's tier plus the five communication gates downstream.
        return 0.6
    name = event.target.rsplit(os.sep, 1)[-1].lower()
    base = {
        EventKind.FILE_CREATED: 0.5,
        EventKind.FILE_MODIFIED: 0.4,
        EventKind.FILE_DELETED: 0.6,  # deletes are more notable than routine edits
        EventKind.FILE_MOVED: 0.5,
    }.get(event.kind, 0.3)
    if name.endswith(_SOURCE_SUFFIXES):
        base += 0.2
    if name in {"makefile", "dockerfile", "readme.md", "pyproject.toml", ".env"}:
        base += 0.2  # project-defining files
    return max(0.0, min(1.0, base))
