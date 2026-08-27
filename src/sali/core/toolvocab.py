"""Canonical vocabulary for Tool Intelligence (spec §3/§5/§6/§24).

The machine, its tools, and their capabilities live in the EXISTING knowledge graph as free-text
`graph_node.node_type` / `graph_edge.rel_type` values. Because those columns are free text, a single
typo would silently fork the graph into duplicate entities — so the canonical strings, `canonical_key`
prefixes, and the controlled capability vocabulary are defined ONCE here and imported everywhere
(the discovery observer, graph traversal, and CLI all agree). This module is pure data + string
helpers with no dependencies, so it sits in `core` and any layer may use it.

`CAPABILITY_VOCAB` is the controlled set of *abstract capabilities a tool provides* (e.g.
`host_discovery`) — the vocabulary Sali reasons over ("I need host discovery" → search capabilities →
tools), per §6/§7. It is DISTINCT from `core.enums.Capability` (read/write/execute/…), which is the
security surface a tool *touches*.
"""

from __future__ import annotations

# ---- graph node types (values for graph_node.node_type) --------------------------------------
NODE_MACHINE = "machine"        # the desktop itself — already minted by the twin (observe_machine)
NODE_EXT_TOOL = "ext_tool"      # a discovered external binary available on the machine
NODE_CAPABILITY = "capability"  # an abstract capability a tool provides
NODE_OS_PACKAGE = "os_package"  # an installed OS package that owns one or more tools

# ---- graph relation types (values for graph_edge.rel_type) -----------------------------------
REL_HAS_TOOL = "has_tool"                        # machine   → ext_tool
REL_PROVIDES_CAPABILITY = "provides_capability"  # ext_tool  → capability
REL_PROVIDED_BY_PACKAGE = "provided_by_package"  # ext_tool  → os_package


# ---- canonical_key builders (hardware/name-stable identity, matching graph_node conventions) ----
def ext_tool_key(name: str) -> str:
    """Stable identity for a discovered tool node, e.g. ``ext_tool:nmap``."""
    return f"{NODE_EXT_TOOL}:{name}"


def capability_key(slug: str) -> str:
    """Stable identity for a capability node, e.g. ``capability:host_discovery``."""
    return f"{NODE_CAPABILITY}:{slug}"


def os_package_key(name: str) -> str:
    """Stable identity for an OS-package node, e.g. ``pkg:nmap``."""
    return f"pkg:{name}"


# ---- controlled capability vocabulary: slug → human description (§6/§24) ----------------------
# A curated, stable seed spanning the Kali security categories plus general dev/sysadmin work.
# Later increments relate discovered tools to these slugs; the set can grow, but slugs never change
# meaning (they are canonical_keys). Slugs are lowercase_snake_case.
CAPABILITY_VOCAB: dict[str, str] = {
    # networking & recon
    "host_discovery": "Find live hosts on a network",
    "port_scanning": "Enumerate open TCP/UDP ports on a host",
    "service_detection": "Identify services/versions behind open ports",
    "packet_capture": "Capture and inspect network traffic",
    "dns_enumeration": "Query and enumerate DNS records",
    "http_enumeration": "Discover paths/parameters on a web service",
    # web & api
    "http_client": "Make HTTP requests to a service or API",
    "web_scraping": "Extract structured data from web pages",
    # credentials & crypto
    "password_cracking": "Recover passwords from hashes or captures",
    "hash_computation": "Compute or verify cryptographic hashes",
    "encryption": "Encrypt/decrypt data or manage keys",
    # exploitation & analysis
    "vulnerability_scanning": "Scan targets for known vulnerabilities",
    "reverse_engineering": "Disassemble or analyze binaries",
    "forensics": "Recover/inspect artifacts from disks or files",
    # data wrangling
    "text_search": "Search text/content across files",
    "file_search": "Find files by name/attributes on the filesystem",
    "json_processing": "Query and transform JSON",
    "data_transformation": "Filter/reshape structured text streams",
    "archiving": "Create or extract compressed archives",
    # multimedia
    "media_transcoding": "Convert/encode audio or video",
    "media_download": "Download media from the web",
    # dev & build
    "version_control": "Track and manage source-code history",
    "compilation": "Compile source code into binaries",
    "package_management": "Install/manage software packages",
    "dependency_resolution": "Resolve and install project dependencies",
    "containerization": "Build/run application containers",
    # system & ops
    "process_inspection": "Inspect running processes and resource usage",
    "service_management": "Start/stop/inspect system services",
    "disk_inspection": "Inspect disks, partitions, and mounts",
    "log_inspection": "Read and query system/service logs",
    "remote_access": "Connect to and operate remote hosts",
}


def is_known_capability(slug: str) -> bool:
    """True if `slug` is part of the controlled capability vocabulary."""
    return slug in CAPABILITY_VOCAB
