"""Authorized file transfer helpers (Prompt 13 §9/§33).

The iPhone never sees a host path. It references an artifact by its id (download) or supplies a bare
filename (upload); the server maps that to a real path and REFUSES anything that resolves outside the
task's authoritative workspace. This keeps Sali's workspace authority intact — the API is a window, never
a bypass (§28/§44). These helpers are pure so they can be unit-tested without a running server.
"""

from __future__ import annotations

import mimetypes
import re
from pathlib import Path

# A generous but bounded upload ceiling (§40). Large transfers still go over HTTP, never WS frames (§33).
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str) -> str | None:
    """Reduce an arbitrary client-supplied name to a single safe path component, or None if impossible.

    Strips directory components and traversal (``../``), collapses unsafe characters, and rejects names
    that are empty or all-dots after sanitizing. The result is always a plain filename — never a path."""
    base = Path(name.strip()).name  # drops any directory part, including "../" segments
    base = _SAFE_NAME.sub("_", base).strip("._")
    if not base or set(base) <= {"_"}:
        return None
    return base[:200]


def is_within(path: Path, root: Path) -> bool:
    """True iff `path`, once resolved, is inside `root` (or is `root`). Symlink-safe via realpath.

    This is the containment gate for every download: an artifact whose recorded path escapes the task
    workspace (a stale record, a symlink, a manually inserted row) is not served."""
    try:
        rp, rr = path.resolve(), root.resolve()
    except (OSError, RuntimeError):
        return False
    return rp == rr or _relative(rp, rr)


def _relative(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def guess_content_type(path: Path) -> str:
    """Best-effort MIME type for a download; defaults to octet-stream (always a safe, non-executing type)."""
    ctype, _ = mimetypes.guess_type(str(path))
    return ctype or "application/octet-stream"
