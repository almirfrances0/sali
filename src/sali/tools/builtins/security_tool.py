"""Security environmental intelligence (spec §24/§25/§78).

Sali connects security knowledge to its ACTUAL machine: which services are exposed to the network,
whether SSH is configured weakly. It reasons about the live posture and INFORMS Almir — it never
disables a service or changes a config on its own (§78: investigate and inform first). Deterministic
checks over real state (ss, sshd_config), read-only.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from sali.core.enums import Capability, RiskLevel
from sali.tools.base import Tool, ToolResult
from sali.tools.context import ToolContext
from sali.tools.exec import run_argv

_LOOPBACK = ("127.", "::1", "[::1]")


def _host_of(local: str) -> str:
    """Extract the bind host from an ss local-address column (e.g. '0.0.0.0:8080' → '0.0.0.0')."""
    if local.startswith("["):  # IPv6 [::]:443
        return local.split("]")[0] + "]"
    return local.rsplit(":", 1)[0] if ":" in local else local


def analyze_ports(ss_out: str) -> list[dict[str, str]]:
    """Findings for services listening on a non-loopback address — i.e. reachable off the machine."""
    findings: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in ss_out.splitlines():
        cols = line.split()
        if len(cols) < 5:
            continue
        local = cols[4]
        host = _host_of(local)
        exposed = host in ("0.0.0.0", "*", "[::]", "::") or not host.startswith(_LOOPBACK)
        if exposed and local not in seen:
            seen.add(local)
            findings.append({
                "severity": "warn", "category": "exposure",
                "summary": f"{cols[0]} service listening on {local} — reachable from the network"})
    return findings


def analyze_sshd(sshd_text: str) -> list[dict[str, str]]:
    """Findings for common weak SSH settings (uncommented directives only)."""
    findings: list[dict[str, str]] = []
    for raw in sshd_text.splitlines():
        line = raw.strip().lower()
        if line.startswith("#") or not line:
            continue
        if line.startswith("permitrootlogin") and line.split()[-1] in ("yes", "prohibit-password"):
            findings.append({"severity": "warn", "category": "ssh_hardening",
                             "summary": f"SSH permits root login ({raw.strip()})"})
        if line.startswith("passwordauthentication") and line.endswith("yes"):
            findings.append({"severity": "warn", "category": "ssh_hardening",
                             "summary": "SSH allows password authentication (keys are stronger)"})
    return findings


class SecurityCheck(Tool):
    name = "security_check"
    description = (
        "Inspect this machine's live security posture — which services are exposed to the network and "
        "whether SSH is configured weakly — and report what you find. Use it when Almir asks about "
        "security, exposure, or hardening. You INFORM about issues; you never disable a service or "
        "change a config on your own. Returns a list of findings (empty means nothing obvious)."
    )
    parameters = {"type": "object", "properties": {}}
    risk_level = RiskLevel.R0
    capabilities = frozenset({Capability.READ})
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        findings: list[dict[str, str]] = []
        try:
            _, ss_out, _ = await run_argv(["ss", "-H", "-lntu"], timeout=8.0)
            findings += analyze_ports(ss_out)
        except (FileNotFoundError, OSError):
            pass
        sshd = Path("/etc/ssh/sshd_config")
        if sshd.is_file():
            with contextlib.suppress(OSError):
                findings += analyze_sshd(sshd.read_text("utf-8", "replace"))
        display = (f"{len(findings)} finding(s)" if findings
                   else "no obvious exposures or weak SSH settings")
        return ToolResult(ok=True, output={"findings": findings}, display=display)


def register_builtins(registry: Any) -> None:
    registry.register(SecurityCheck())
