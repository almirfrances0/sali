"""JSON-safe serialization for the web boundary (§34).

Two responsibilities, always applied together via ``safe()``:
1. Turn Sali's internal objects (dataclasses, UUIDs, datetimes, enums, sets) into JSON primitives.
2. Redact secrets on the way out — `event.payload`, `memory.content`, tool plans and twin props are NOT
   redacted at rest, so every value that leaves for a browser passes through `redact_obj` here. The web
   is an interface, never a security bypass: the browser never receives a raw secret because Sali knows it.
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from sali.security.redact import redact_obj


def to_jsonable(obj: Any) -> Any:
    """Recursively convert an internal object graph into JSON primitives (no redaction yet)."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, UUID):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return to_jsonable(obj.value)
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in obj]
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        return to_jsonable(to_dict())
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    return str(obj)  # last resort — never leak an opaque repr with internal state


def safe(obj: Any) -> Any:
    """JSON-safe AND redacted — the only converter a route should use before returning to a browser."""
    return redact_obj(to_jsonable(obj))
