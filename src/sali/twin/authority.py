"""Deterministic execution-authority classification (spec §34/§35).

Every discovered tool is assigned an authority TIER — NORMAL / ELEVATED / SYSTEM_CRITICAL — by a
deterministic, name-based classifier (no model). The tier is written to `tool_authority` as an
immutable, temporal, provenance-carrying record: a reclassification closes the old row and inserts a
new current one — history is never overwritten.

This increment RECORDS only; nothing is gated yet. The record is the immutable boundary a later
increment reads: the tier maps to a RiskLevel/Capability (core.enums.authority_risk) that the single
PolicyEngine gate enforces, so SYSTEM_CRITICAL (→ R4) is the only tier that pauses to confirm, while
NORMAL/ELEVATED stay autonomous (the 'freedom, not restricted' philosophy). The classifier is coarse
by design — a per-tool FLOOR; a specific invocation's danger is refined per-call at execution time
(the same discipline as exec_tool's command-level _DESTRUCTIVE check, which this is consistent with).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sali.core.enums import ExecAuthority, MemorySource, authority_capabilities
from sali.obs.log import get_logger

log = get_logger("sali.twin.authority")

# Tools whose PURPOSE is destructive/irreversible system, disk, or power-state operations — no safe
# routine use in Sali's normal workflow. These reach the immutable confirm boundary. (Consistent with
# exec_tool._DESTRUCTIVE, which additionally catches dangerous INVOCATIONS of otherwise-normal tools.)
_SYSTEM_CRITICAL: frozenset[str] = frozenset({
    "mkfs", "mke2fs", "mkfs.ext4", "mkfs.vfat", "mkfs.ntfs", "mkfs.xfs", "mkfs.btrfs",
    "fdisk", "sfdisk", "cfdisk", "parted", "sgdisk", "gdisk", "wipefs", "shred", "blkdiscard",
    "cryptsetup", "mkswap", "fsck", "e2fsck", "badblocks",
    "shutdown", "reboot", "halt", "poweroff",
})

# Tools that operate with system/root privilege or change system state, but have legitimate routine
# use. Autonomous under the freedom policy; tiered ELEVATED for awareness and audit.
_ELEVATED: frozenset[str] = frozenset({
    "apt", "apt-get", "aptitude", "dpkg", "snap", "flatpak",
    "systemctl", "service", "systemd", "journalctl",
    "mount", "umount", "swapon", "swapoff", "losetup",
    "useradd", "adduser", "usermod", "groupadd", "gpasswd", "passwd", "chpasswd",
    "visudo", "sudo", "su", "chown", "chgrp",
    "iptables", "ip6tables", "nft", "ufw", "arptables", "ebtables",
    "modprobe", "insmod", "rmmod", "depmod", "sysctl",
    "update-grub", "grub-install", "grub-mkconfig", "efibootmgr",
    "crontab", "timedatectl", "hostnamectl", "localectl", "update-alternatives",
    "dd",  # capable of catastrophic writes; dangerous invocations refine higher per-call
})


def classify_authority(name: str) -> tuple[ExecAuthority, str]:
    """The execution-authority tier for a tool, by name. Deterministic; returns (tier, rationale)."""
    base = name.lower()
    if base in _SYSTEM_CRITICAL:
        return ExecAuthority.SYSTEM_CRITICAL, "destructive/irreversible system, disk, or power operation"
    if base in _ELEVATED:
        return ExecAuthority.ELEVATED, "operates with system/root privilege or changes system state"
    return ExecAuthority.NORMAL, "everyday tool with no inherent system-critical effect"


@dataclass(slots=True)
class AuthorityResult:
    classified: int        # available tools examined
    normal: int
    elevated: int
    system_critical: int
    written: int           # tools given a NEW or reclassified current authority this pass


async def classify_tools(
    conn: Any, *, source: MemorySource = MemorySource.EXTERNAL_SOURCE
) -> AuthorityResult:
    """Classify every available tool and record its authority immutably. Idempotent: an unchanged
    tier is a no-op; a changed tier supersedes (closes the old current row, inserts a new one).
    Caller owns the transaction."""
    rows = await conn.fetch("SELECT id, name FROM discovered_tool WHERE available")
    counts = {ExecAuthority.NORMAL: 0, ExecAuthority.ELEVATED: 0, ExecAuthority.SYSTEM_CRITICAL: 0}
    written = 0
    for row in rows:
        tier, rationale = classify_authority(row["name"])
        counts[tier] += 1
        current = await conn.fetchrow(
            "SELECT id, authority FROM tool_authority WHERE tool_id=$1 AND valid_until IS NULL",
            row["id"])
        if current is not None and current["authority"] == tier.value:
            continue  # already classified this way — leave the immutable record alone
        if current is not None:  # a reclassification — supersede, never overwrite
            await conn.execute("UPDATE tool_authority SET valid_until=now() WHERE id=$1", current["id"])
        await conn.execute(
            "INSERT INTO tool_authority (tool_id, authority, rationale, classifier, capabilities, "
            "  source, confidence) VALUES ($1,$2,$3,'deterministic',$4,$5::memory_source,$6)",
            row["id"], tier.value, rationale,
            sorted(c.value for c in authority_capabilities(tier)), source.value, 0.9)
        written += 1

    await conn.execute(
        "INSERT INTO event (event_type, payload) VALUES ('tool.authority_classified', $1)",
        {"classified": len(rows), "normal": counts[ExecAuthority.NORMAL],
         "elevated": counts[ExecAuthority.ELEVATED],
         "system_critical": counts[ExecAuthority.SYSTEM_CRITICAL], "written": written})
    return AuthorityResult(
        classified=len(rows), normal=counts[ExecAuthority.NORMAL],
        elevated=counts[ExecAuthority.ELEVATED],
        system_critical=counts[ExecAuthority.SYSTEM_CRITICAL], written=written)


async def current_authority(conn: Any, name: str) -> ExecAuthority | None:
    """The current authority tier recorded for a tool, or None if unclassified/unknown."""
    value = await conn.fetchval(
        "SELECT a.authority FROM tool_authority a JOIN discovered_tool t ON t.id=a.tool_id "
        "WHERE t.name=$1 AND a.valid_until IS NULL", name)
    return ExecAuthority(value) if value is not None else None


async def set_authority(
    conn: Any, name: str, tier: ExecAuthority, *, rationale: str = "", classifier: str = "user",
    source: MemorySource = MemorySource.INFERENCE,
) -> bool:
    """Record a new current authority for a tool by name, superseding any prior (never overwriting).
    Returns False if the tool isn't in the inventory; True if set or already at this tier."""
    tool_id = await conn.fetchval("SELECT id FROM discovered_tool WHERE name=$1", name)
    if tool_id is None:
        return False
    current = await conn.fetchrow(
        "SELECT id, authority FROM tool_authority WHERE tool_id=$1 AND valid_until IS NULL", tool_id)
    if current is not None and current["authority"] == tier.value:
        return True
    if current is not None:
        await conn.execute("UPDATE tool_authority SET valid_until=now() WHERE id=$1", current["id"])
    await conn.execute(
        "INSERT INTO tool_authority (tool_id, authority, rationale, classifier, capabilities, "
        "  source, confidence) VALUES ($1,$2,$3,$4,$5,$6::memory_source,$7)",
        tool_id, tier.value, rationale, classifier,
        sorted(c.value for c in authority_capabilities(tier)), source.value, 0.8)
    return True
