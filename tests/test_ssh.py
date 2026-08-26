"""SSH / VPS tools (§44): destructive-remote escalation, the runner, and the fake."""

from __future__ import annotations

from typing import Any

import pytest

from sali.config.secrets import FakeSecretStore
from sali.config.settings import Settings, SshSettings
from sali.core.clock import SystemClock
from sali.core.enums import RiskLevel
from sali.tools.builtins.ssh import SshPut, SshRun, _is_destructive_remote
from sali.tools.context import ToolContext
from sali.tools.remote import (
    FakeRemoteRunner,
    RemoteResult,
    SshRunner,
    build_remote_runner,
    password_ref,
)


def test_destructive_remote_detection() -> None:
    for safe in ("df -h", "systemctl status nginx", "tail -n 100 /var/log/syslog", "git pull"):
        assert not _is_destructive_remote(safe)
    for danger in ("rm -rf /data", "systemctl stop postgres", "dropdb prod", "mkfs.ext4 /dev/sdb",
                   "iptables -F", "shutdown -h now"):
        assert _is_destructive_remote(danger)


def test_ssh_run_escalates_only_destructive() -> None:
    assert SshRun().assess({"command": "df -h"}) is RiskLevel.R1  # ordinary → free
    assert SshRun().assess({"command": "rm -rf /srv"}) is RiskLevel.R4  # destructive → confirm


def test_remote_result_ok_distinguishes_ssh_failure_from_command_exit() -> None:
    assert RemoteResult("h", 0, "out", "").ok
    assert RemoteResult("h", 1, "", "no match").ok  # a non-zero remote exit is data, not a failure
    assert not RemoteResult("h", 255, "", "connection refused").ok  # ssh-level failure


def _ctx(runner: FakeRemoteRunner) -> ToolContext:
    return ToolContext(settings=Settings(), clock=SystemClock(), remote=runner)


async def test_ssh_run_tool_returns_remote_output() -> None:
    runner = FakeRemoteRunner({("vps1", "df"): RemoteResult("vps1", 0, "/ 42% used", "")})
    res = await SshRun().run({"host": "vps1", "command": "df -h"}, _ctx(runner))
    assert res.ok and res.output["stdout"] == "/ 42% used" and res.output["returncode"] == 0
    assert runner.calls == [("vps1", "df -h")]


async def test_ssh_run_tool_reports_connection_failure() -> None:
    runner = FakeRemoteRunner({("down", "uptime"): RemoteResult("down", 255, "", "Connection refused")})
    res = await SshRun().run({"host": "down", "command": "uptime"}, _ctx(runner))
    assert not res.ok and "refused" in (res.error or "").lower()


async def test_ssh_put_tool_copies() -> None:
    runner = FakeRemoteRunner()
    res = await SshPut().run(
        {"local": "/tmp/a.txt", "host": "vps1", "remote": "/srv/a.txt"}, _ctx(runner))
    assert res.ok and runner.calls == [("vps1", "put /tmp/a.txt -> /srv/a.txt")]


async def test_ssh_tools_guard_missing_runner_and_args() -> None:
    no_runner = await SshRun().run({"host": "h", "command": "ls"},
                                   ToolContext(settings=Settings(), clock=SystemClock()))
    assert not no_runner.ok and "available" in (no_runner.error or "")
    bad = await SshRun().run({"host": "", "command": "ls"}, _ctx(FakeRemoteRunner()))
    assert not bad.ok


def test_build_remote_runner_selects_backend() -> None:
    assert isinstance(build_remote_runner(SshSettings(backend="fake")), FakeRemoteRunner)
    assert isinstance(build_remote_runner(SshSettings(backend="ssh")), SshRunner)


# ── password auth over the vault (sshpass) ────────────────────────────────────

def test_password_ref_is_stable_across_usernames() -> None:
    assert password_ref("almir@74.207.227.95") == "ssh.74_207_227_95.password"
    assert password_ref("root@74.207.227.95") == "ssh.74_207_227_95.password"  # same host, any user
    assert password_ref("myvps") == "ssh.myvps.password"  # a config alias


async def test_runner_uses_sshpass_with_password_never_on_the_command_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_run_argv(argv: list[str], *, timeout: float = 8.0,
                            env: dict[str, str] | None = None) -> tuple[int, str, str]:
        captured["argv"] = argv
        captured["env"] = env or {}
        return (0, "ok", "")

    monkeypatch.setattr("sali.tools.remote.run_argv", fake_run_argv)
    runner = SshRunner(secrets=FakeSecretStore({"ssh.1_2_3_4.password": "hunter2"}))
    res = await runner.run("almir@1.2.3.4", "uname -a")
    assert res.ok
    assert captured["argv"][:3] == ["sshpass", "-e", "ssh"]  # password fed via SSHPASS env, not argv
    assert captured["env"].get("SSHPASS") == "hunter2"
    assert "hunter2" not in " ".join(captured["argv"])  # never on the command line / in logs


async def test_runner_persists_an_inline_password_to_the_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_argv(argv: list[str], *, timeout: float = 8.0,
                            env: dict[str, str] | None = None) -> tuple[int, str, str]:
        return (0, "", "")

    monkeypatch.setattr("sali.tools.remote.run_argv", fake_run_argv)
    store = FakeSecretStore()
    runner = SshRunner(secrets=store)
    await runner.run("almir@1.2.3.4", "whoami", password="maxmasaka")
    assert store.get(password_ref("almir@1.2.3.4")) == "maxmasaka"  # saved for next time, encrypted


async def test_runner_falls_back_to_key_auth_without_a_password(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_run_argv(argv: list[str], *, timeout: float = 8.0,
                            env: dict[str, str] | None = None) -> tuple[int, str, str]:
        captured["argv"] = argv
        return (0, "", "")

    monkeypatch.setattr("sali.tools.remote.run_argv", fake_run_argv)
    runner = SshRunner(secrets=FakeSecretStore())  # no stored password for this host
    await runner.run("keyhost", "uptime")
    assert captured["argv"][0] == "ssh" and "BatchMode=yes" in captured["argv"]  # key-only path


async def test_ssh_tool_passes_password_through_to_the_runner() -> None:
    runner = FakeRemoteRunner()
    ctx = ToolContext(settings=Settings(), clock=SystemClock(), remote=runner)
    res = await SshRun().run(
        {"host": "vps", "command": "uname", "password": "maxmasaka"}, ctx)
    assert res.ok and runner.passwords == ["maxmasaka"]  # routed to the runner, not into the command
