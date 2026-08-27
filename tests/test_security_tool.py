"""Phase 3 · Increment 11 — security environmental intelligence (§24/§25/§78).

Sali reasons about the live machine's exposure: a service on a non-loopback address is reachable off
the machine; weak SSH settings are flagged. Deterministic; it informs, never acts.
"""

from __future__ import annotations

from sali.config.settings import Settings
from sali.core.clock import SystemClock
from sali.tools.builtins.security_tool import SecurityCheck, analyze_ports, analyze_sshd
from sali.tools.context import ToolContext


def test_exposed_ports_are_flagged_loopback_is_not() -> None:
    ss = (
        "tcp LISTEN 0 128 0.0.0.0:8080 0.0.0.0:*\n"       # exposed (all interfaces)
        "tcp LISTEN 0 128 127.0.0.1:11434 0.0.0.0:*\n"    # local only — fine
        "tcp LISTEN 0 128 [::]:443 [::]:*\n"              # exposed (IPv6 any)
        "tcp LISTEN 0 128 [::1]:631 [::]:*\n"             # loopback IPv6 — fine
    )
    findings = analyze_ports(ss)
    exposed = {f["summary"] for f in findings}
    assert any("0.0.0.0:8080" in s for s in exposed)
    assert any("[::]:443" in s for s in exposed)
    assert not any("11434" in s for s in exposed)  # loopback not flagged
    assert not any("631" in s for s in exposed)
    assert all(f["category"] == "exposure" for f in findings)


def test_weak_ssh_settings_are_flagged() -> None:
    cfg = (
        "# hardened example\n"
        "PermitRootLogin yes\n"
        "PasswordAuthentication yes\n"
        "#PasswordAuthentication no\n"     # commented → ignored
        "Port 22\n"
    )
    findings = analyze_sshd(cfg)
    cats = [f["summary"] for f in findings]
    assert any("root login" in s for s in cats)
    assert any("password authentication" in s for s in cats)
    assert len(findings) == 2  # the commented line is not counted


def test_clean_sshd_has_no_findings() -> None:
    assert analyze_sshd("PermitRootLogin no\nPasswordAuthentication no\n") == []


async def test_security_check_tool_runs_and_reports() -> None:
    # runs against the real machine — asserts the shape/contract, not specific findings
    ctx = ToolContext(settings=Settings(), clock=SystemClock())
    res = await SecurityCheck().run({}, ctx)
    assert res.ok and "findings" in res.output
    assert isinstance(res.output["findings"], list)
    for f in res.output["findings"]:
        assert f["category"] in ("exposure", "ssh_hardening") and f["severity"] == "warn"
