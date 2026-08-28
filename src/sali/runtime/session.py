"""The single continuous session.

Sali doesn't have separate chats — there is one ongoing conversation that every front-end
(terminal, WebSocket, future app) shares, so it always picks up where it left off.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid5

from sali.core.ids import new_id

_SESSION_FILE = Path.home() / ".local" / "share" / "sali" / "session"


def persistent_session_id() -> UUID:
    _SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _SESSION_FILE.exists():
        try:
            return UUID(_SESSION_FILE.read_text().strip())
        except ValueError:
            pass
    session = new_id()
    _SESSION_FILE.write_text(str(session))
    return session


def background_session_id() -> UUID:
    """A DISTINCT conversation for Sali's OWN autonomous turns — attention-driven investigations and
    scheduled jobs (§14/§15). Kept separate from Almir's terminal conversation so a background port/socket
    investigation can never write into — and hijack the referents of — the active user task. Derived
    deterministically from the user session so it's stable across restarts but never collides with it."""
    return uuid5(persistent_session_id(), "sali-background")
