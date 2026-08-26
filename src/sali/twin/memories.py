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
    """Return ``(claim_key, content)`` per facet — the durable, retrievable natural-language view of
    the machine. Static facets (software/models/projects/envs) are skipped when empty; the volatile
    membership facets (services/containers/network) are ALWAYS emitted, with an explicit empty
    phrasing, so a functional re-observation retracts a value that's no longer true."""
    out: list[tuple[str, str]] = []
    props = snapshot.machine_props or {}

    # Each hardware bit is prefixed with its category word (CPU/RAM/GPU/disk) so a plain keyword
    # question — "what gpu do i have" — actually matches, even when the device name ("RTX 4070")
    # doesn't contain the word.
    hw_labels = (("hw:cpu", "CPU"), ("hw:memory", "RAM"), ("hw:gpu", "GPU"), ("hw:storage:root", "disk"))
    hw_bits = [f"{label}: {name}" for key, label in hw_labels if (name := _find(snapshot, key))]
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

    # Membership facets that CHANGE over time (a service stops, a container exits, an interface goes
    # down). These are ALWAYS emitted — with an explicit empty phrasing — so a functional
    # re-observation RETRACTS the old value ("containers: X" → "no containers") instead of leaving a
    # stale fact current forever. Category words are baked in for keyword recall.
    services = _names(snapshot, "service")
    out.append(("twin:services",
                ("Services running on this machine: " + ", ".join(services) + ".") if services
                else "No tracked services are currently running on this machine."))

    containers = _names(snapshot, "container")
    out.append(("twin:containers",
                ("Docker containers currently running: " + ", ".join(containers) + ".") if containers
                else "No Docker containers are currently running."))

    net_bits = [
        f"{e.name} {ip}" if (ip := e.props.get("ip")) else e.name
        for e in snapshot.by_kind("network")
    ]
    out.append(("twin:network",
                ("Network interfaces and IP addresses: " + ", ".join(net_bits) + ".") if net_bits
                else "No active network interfaces on this machine right now."))

    envs = _names(snapshot, "environment")
    if envs:
        out.append(("twin:environments", "Python virtual environments here: " + ", ".join(envs) + "."))
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
