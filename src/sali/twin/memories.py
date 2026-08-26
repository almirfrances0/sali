"""Turn a twin snapshot into system-environment memories (spec §6-F + §14).

The graph holds the twin's *structure*; these memories hold the same facts in plain language so
semantic recall grounds ordinary questions — "what GPU do I have", "how much RAM", "what's
installed" — without depending on exact wording or graph-traversal direction. Each facet is one
*functional* claim (a stable ``claim_key``), so a re-observation supersedes the previous value
instead of piling up duplicates: the memory stays single-current and always fresh. Deterministic
text, never model-written (§15).
"""

from __future__ import annotations

from typing import Any

from sali.core.enums import MemoryLayer, MemorySource
from sali.memory import writer as memory_writer
from sali.twin.model import TwinSnapshot


def _names(snapshot: TwinSnapshot, kind: str) -> list[str]:
    return [e.name for e in snapshot.by_kind(kind)]


def _find(snapshot: TwinSnapshot, key: str) -> str | None:
    return next((e.name for e in snapshot.entities if e.key == key), None)


def build_twin_memories(snapshot: TwinSnapshot) -> list[tuple[str, str]]:
    """Return ``(claim_key, content)`` for each non-empty facet — the durable, retrievable
    natural-language view of the machine. Empty facets are skipped."""
    out: list[tuple[str, str]] = []
    props = snapshot.machine_props or {}

    hw_bits = [
        b for b in (
            _find(snapshot, "hw:cpu"), _find(snapshot, "hw:memory"),
            _find(snapshot, "hw:gpu"), _find(snapshot, "hw:storage:root"),
        ) if b
    ]
    kernel = props.get("kernel", "")
    arch = props.get("arch", "")
    machine = (
        f"This is Almir's machine: {snapshot.machine_name}"
        + (f" (kernel {kernel}, {arch})" if kernel or arch else "")
        + (". Its hardware — " + "; ".join(hw_bits) + "." if hw_bits else ".")
    )
    out.append(("twin:machine", machine))

    software = _names(snapshot, "software")
    if software:
        out.append(("twin:software", "Software installed on this machine: " + ", ".join(software) + "."))

    models = _names(snapshot, "model")
    if models:
        out.append(("twin:models", "Ollama models available locally: " + ", ".join(models) + "."))

    projects = [
        f"{e.name} (at {e.props.get('path')})" if e.props.get("path") else e.name
        for e in snapshot.by_kind("project")
    ]
    if projects:
        out.append(("twin:projects", "Git projects on this machine: " + ", ".join(projects) + "."))
    return out


async def write_twin_memories(conn: Any, snapshot: TwinSnapshot) -> int:
    """Write each facet as a functional system-env memory (supersedes its prior value). Caller
    owns the transaction; embedding happens afterwards via the memory service."""
    memories = build_twin_memories(snapshot)
    for claim_key, content in memories:
        await memory_writer.remember(
            conn, layer=MemoryLayer.SYSTEM_ENV, content=content,
            source=MemorySource.SYSTEM_OBSERVATION, functional=True, claim_key=claim_key,
            importance=0.6,
        )
    return len(memories)
