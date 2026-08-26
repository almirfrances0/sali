"""Identifier generation.

Prefer time-ordered UUIDv7 (stdlib in Python 3.14) so ids sort by creation time,
which keeps B-tree indexes on primary keys locality-friendly. Fall back to uuid4.
"""

from __future__ import annotations

import uuid


def new_id() -> uuid.UUID:
    """Return a fresh, preferably time-ordered, UUID."""
    gen = getattr(uuid, "uuid7", None)
    if gen is not None:
        return gen()  # type: ignore[no-any-return]
    return uuid.uuid4()
