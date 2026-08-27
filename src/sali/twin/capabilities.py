"""Deterministic tool→capability mapping (spec §6/§7/§21).

Knowing a binary exists isn't enough — Sali must reason "I need host discovery" → which tools
provide it (§6/§7). This seeds the controlled capability vocabulary as graph nodes and relates each
KNOWN discovered tool to the capabilities it provides, via a hand-curated rules table. It is
deterministic (no model): the rules are built-in, documentation-derived knowledge. Tools with no
rule simply carry no capability yet — an honest gap a later increment can fill by interpretation.

Because capabilities are graph nodes and `provides_capability` edges, alternatives fall out for free
(§21): two tools sharing a capability are alternatives. Only tools actually present on the machine
(available in the inventory, with a graph node) get edges.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sali.core.enums import MemorySource
from sali.core.toolvocab import (
    CAPABILITY_VOCAB,
    NODE_CAPABILITY,
    REL_PROVIDES_CAPABILITY,
    capability_key,
)
from sali.graph.writer import ensure_node, relate
from sali.obs.log import get_logger

log = get_logger("sali.twin.capabilities")

# Curated known-binary → capabilities. Every slug MUST be in CAPABILITY_VOCAB (enforced by a test).
# This is deterministic built-in knowledge, not a live observation, so it is recorded as
# EXTERNAL_SOURCE — out-ranked by a real observation, but above a mere inference.
CAPABILITY_RULES: dict[str, frozenset[str]] = {
    # networking / recon
    "nmap": frozenset({"host_discovery", "port_scanning", "service_detection"}),
    "masscan": frozenset({"host_discovery", "port_scanning"}),
    "rustscan": frozenset({"port_scanning"}),
    "nc": frozenset({"port_scanning"}),
    "ncat": frozenset({"port_scanning"}),
    "tcpdump": frozenset({"packet_capture"}),
    "tshark": frozenset({"packet_capture"}),
    "wireshark": frozenset({"packet_capture"}),
    "dnsrecon": frozenset({"dns_enumeration"}),
    "dnsenum": frozenset({"dns_enumeration"}),
    "dig": frozenset({"dns_enumeration"}),
    "host": frozenset({"dns_enumeration"}),
    "gobuster": frozenset({"http_enumeration"}),
    "ffuf": frozenset({"http_enumeration"}),
    "dirb": frozenset({"http_enumeration"}),
    "nikto": frozenset({"vulnerability_scanning", "http_enumeration"}),
    "wpscan": frozenset({"vulnerability_scanning", "http_enumeration"}),
    # web / api
    "curl": frozenset({"http_client"}),
    "wget": frozenset({"http_client", "media_download"}),
    "httpie": frozenset({"http_client"}),
    # credentials / crypto
    "hashcat": frozenset({"password_cracking"}),
    "john": frozenset({"password_cracking"}),
    "hydra": frozenset({"password_cracking"}),
    "sha256sum": frozenset({"hash_computation"}),
    "md5sum": frozenset({"hash_computation"}),
    "openssl": frozenset({"encryption", "hash_computation"}),
    "gpg": frozenset({"encryption"}),
    # exploitation / analysis
    "sqlmap": frozenset({"vulnerability_scanning"}),
    "radare2": frozenset({"reverse_engineering"}),
    "gdb": frozenset({"reverse_engineering"}),
    "objdump": frozenset({"reverse_engineering"}),
    "binwalk": frozenset({"forensics", "reverse_engineering"}),
    "foremost": frozenset({"forensics"}),
    # data wrangling
    "grep": frozenset({"text_search"}),
    "rg": frozenset({"text_search"}),
    "ag": frozenset({"text_search"}),
    "find": frozenset({"file_search"}),
    "fd": frozenset({"file_search"}),
    "locate": frozenset({"file_search"}),
    "jq": frozenset({"json_processing"}),
    "yq": frozenset({"json_processing", "data_transformation"}),
    "awk": frozenset({"data_transformation"}),
    "sed": frozenset({"data_transformation"}),
    "tar": frozenset({"archiving"}),
    "zip": frozenset({"archiving"}),
    "unzip": frozenset({"archiving"}),
    "7z": frozenset({"archiving"}),
    "gzip": frozenset({"archiving"}),
    # multimedia
    "ffmpeg": frozenset({"media_transcoding"}),
    "yt-dlp": frozenset({"media_download"}),
    # dev / build
    "git": frozenset({"version_control"}),
    "gcc": frozenset({"compilation"}),
    "clang": frozenset({"compilation"}),
    "make": frozenset({"compilation"}),
    "cargo": frozenset({"compilation", "dependency_resolution", "package_management"}),
    "rustc": frozenset({"compilation"}),
    "go": frozenset({"compilation", "dependency_resolution"}),
    "apt": frozenset({"package_management"}),
    "apt-get": frozenset({"package_management"}),
    "dpkg": frozenset({"package_management"}),
    "pip": frozenset({"package_management", "dependency_resolution"}),
    "pip3": frozenset({"package_management", "dependency_resolution"}),
    "pipx": frozenset({"package_management"}),
    "npm": frozenset({"package_management", "dependency_resolution"}),
    "uv": frozenset({"package_management", "dependency_resolution"}),
    "docker": frozenset({"containerization"}),
    "podman": frozenset({"containerization"}),
    # system / ops
    "ps": frozenset({"process_inspection"}),
    "top": frozenset({"process_inspection"}),
    "htop": frozenset({"process_inspection"}),
    "btop": frozenset({"process_inspection"}),
    "systemctl": frozenset({"service_management"}),
    "lsblk": frozenset({"disk_inspection"}),
    "df": frozenset({"disk_inspection"}),
    "fdisk": frozenset({"disk_inspection"}),
    "journalctl": frozenset({"log_inspection"}),
    "dmesg": frozenset({"log_inspection"}),
    "ssh": frozenset({"remote_access"}),
    "sshpass": frozenset({"remote_access"}),
    "scp": frozenset({"remote_access"}),
    "rsync": frozenset({"remote_access"}),
}


@dataclass(slots=True)
class CapabilityResult:
    capabilities: int   # capability vocabulary nodes ensured
    tools_mapped: int   # discovered tools that matched a rule
    edges: int          # provides_capability edges asserted


async def apply_capabilities(
    conn: Any, *, source: MemorySource = MemorySource.EXTERNAL_SOURCE
) -> CapabilityResult:
    """Seed capability nodes and relate every KNOWN available tool to its capabilities. Idempotent
    (relate dedups). Caller owns the transaction."""
    cap_nodes: dict[str, Any] = {}
    for slug, desc in CAPABILITY_VOCAB.items():
        node = await ensure_node(
            conn, node_type=NODE_CAPABILITY, name=slug, canonical_key=capability_key(slug),
            source=source, props={"description": desc})
        cap_nodes[slug] = node.id

    tools_mapped = 0
    edges = 0
    for binary, slugs in CAPABILITY_RULES.items():
        row = await conn.fetchrow(
            "SELECT node_id FROM discovered_tool WHERE name=$1 AND available AND node_id IS NOT NULL",
            binary)
        if row is None:
            continue  # tool not installed here (or not yet in the graph) — honest gap, no edge
        tools_mapped += 1
        for slug in slugs:
            await relate(conn, src_id=row["node_id"], dst_id=cap_nodes[slug],
                         rel_type=REL_PROVIDES_CAPABILITY, source=source)
            edges += 1
    return CapabilityResult(capabilities=len(cap_nodes), tools_mapped=tools_mapped, edges=edges)


async def tools_with_capability(conn: Any, slug: str) -> list[str]:
    """Names of available tools that provide capability ``slug`` — a structural graph lookup (§7)."""
    rows = await conn.fetch(
        "SELECT t.name FROM graph_edge e "
        "  JOIN graph_node cap ON cap.id = e.dst_id "
        "  JOIN discovered_tool t ON t.node_id = e.src_id "
        "WHERE e.rel_type=$1 AND e.valid_until IS NULL AND e.superseded_by IS NULL "
        "  AND cap.canonical_key=$2 AND cap.valid_until IS NULL AND t.available "
        "ORDER BY t.name",
        REL_PROVIDES_CAPABILITY, capability_key(slug))
    return [r["name"] for r in rows]
