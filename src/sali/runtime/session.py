"""The single continuous session.

Sali doesn't have separate chats — there is one ongoing conversation that every front-end
(terminal, WebSocket, future app) shares, so it always picks up where it left off.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

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
