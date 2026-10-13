"""Memory scope derivation (spec §30/§31).

A memory can belong to a scope so retrieval surfaces the CURRENT project's knowledge heavily without
flooding the context with unrelated projects. The one derivation everything shares: a filesystem
location's project is the git repository it lives in — pure, deterministic, no I/O beyond a stat.
"""

from __future__ import annotations

import contextlib
from pathlib import Path


def project_scope(path: str) -> str | None:
    """'project:<repo>' if `path` lives inside a git repository, else None (→ global). Works for a
    directory (a working dir) or a file (a document being ingested)."""
    if not path:
        return None
    with contextlib.suppress(OSError, RuntimeError):
        base = Path(path).expanduser()
        start = base if base.is_dir() else base.parent
        for d in [start, *start.parents]:
            if (d / ".git").exists():
                return f"project:{d.name}"
    return None
