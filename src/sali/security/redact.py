"""Best-effort secret redaction at process boundaries (logs, audit, model context)."""

from __future__ import annotations

import re
from typing import Any

_PATTERNS = [
    re.compile(r"[a-z][a-z0-9+.\-]*://[^\s\"']*:[^\s\"'@/]+@[^\s\"']+", re.IGNORECASE),  # creds in any URL
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),  # email
    re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization)\b\s*[=:]\s*\S+"),
    re.compile(r"(?i)--password[=\s]+\S+"),  # --password <val> / --password=<val>
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+"),  # bearer tokens
    # Passwords passed inline on a command line (e.g. a terminal's window title). Command-anchored so
    # benign attached flags (`tar -pxzf`, `mkdir -p`) are never touched (§27 defense-in-depth).
    re.compile(r"(?i)\b(?:mysql|mysqldump|mariadb|psql)\b[^\n]*?\s-p\S+"),  # mysql -p<secret>
    re.compile(r"(?i)\bredis-cli\b[^\n]*?\s-a\s+\S+"),  # redis-cli -a <secret>
    re.compile(r"(?i)\bcurl\b[^\n]*?\s-u\s+\S+:\S+"),  # curl -u <user>:<pass>
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}"),  # GitHub tokens
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"),
]
_REDACTED = "[redacted]"


def redact(text: str) -> str:
    for pattern in _PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return text


def looks_like_secret(text: str) -> bool:
    """True if `text` contains something the redactor recognizes as a secret (§28 secret detection).
    Cheap and conservative — the same patterns redact() scrubs — for guarding a write ("that looks
    like a credential; store it in the vault, don't put it in memory") before anything is persisted."""
    return redact(text) != text


def redact_obj(obj: Any) -> Any:
    """Recursively redact strings inside dicts/lists; also masks keys named like secrets."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        for key, value in obj.items():
            if isinstance(key, str) and re.search(
                r"(?i)pass|secret|token|api[_-]?key|dsn", key
            ):
                out[key] = _REDACTED
            else:
                out[key] = redact_obj(value)
        return out
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    return obj
