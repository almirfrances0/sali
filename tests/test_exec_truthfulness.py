"""He can run anything. These are the reasons he could not always trust what came back.

Every case is measured, from the tool-capability audit of 2026-09-07:
  `grep sudo /var/log/auth.log`  -> `grep sudo --askpass /var/log/auth.log`  ("unrecognized option")
  `nosuchbin 2>/dev/null | wc -l` -> ok=True, rc=0
  a 400 KB stdout                 -> cut at 256 KiB with nothing saying so
  sandbox=True + background=True  -> ran on the REAL host, and verify() called it success
"""

from __future__ import annotations

import os

import pytest

from sali.tools.builtins.exec_tool import ExecuteCommand
from sali.tools.context import local_context
from sali.tools.privilege import _SUDO_RE

# ── the sudo rewrite must only touch sudo-the-command ────────────────────────────────────────────

@pytest.mark.parametrize("command", [
    "grep sudo /var/log/auth.log",      # he could not grep his own auth log
    "apt install sudo",
    "ls /var/lib/sudo",
    "usermod -aG sudo newuser",
    "journalctl -u sudo --since today",
    "echo 'install sudo first'",
    "cat /etc/sudoers.d/almir-nopasswd",
])
def test_a_mention_of_sudo_is_not_rewritten(command: str) -> None:
    assert _SUDO_RE.search(command) is None, f"would corrupt: {command}"


@pytest.mark.parametrize("command", [
    "sudo apt update",
    "cd /tmp && sudo id",
    "ls; sudo whoami",
    "true | sudo tee /tmp/x",
    "(sudo id)",
])
def test_sudo_in_command_position_is_rewritten(command: str) -> None:
    assert _SUDO_RE.search(command) is not None, f"would not elevate: {command}"


def test_an_already_wrapped_sudo_is_left_alone() -> None:
    assert _SUDO_RE.search("sudo --askpass id") is None
    assert _SUDO_RE.search("sudo -A id") is None


# ── failure must be visible ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_failed_pipeline_stage_fails_the_command() -> None:
    """Without pipefail the status is the LAST stage only, so a missing binary reported success —
    and that is what kept the sudo corruption above invisible."""
    res = await ExecuteCommand().run(
        {"command": "nosuchbinary_xyz 2>/dev/null | wc -l", "timeout": 30}, local_context())
    assert res.ok is False


@pytest.mark.asyncio
async def test_sigpipe_from_head_is_not_a_failure() -> None:
    """pipefail turns the SIGPIPE that `head` inflicts on its upstream into rc=141. `yes | head` is a
    normal successful command and must not start reading as an error."""
    res = await ExecuteCommand().run(
        {"command": "yes 2>/dev/null | head -3", "timeout": 30}, local_context())
    assert res.ok is True
    assert (res.output or {}).get("sigpipe") is True


@pytest.mark.asyncio
async def test_a_genuinely_good_command_still_succeeds() -> None:
    res = await ExecuteCommand().run({"command": "echo hi | cat", "timeout": 30}, local_context())
    assert res.ok is True and "hi" in (res.output or {}).get("stdout", "")


@pytest.mark.asyncio
async def test_a_complaint_on_a_successful_command_is_surfaced() -> None:
    res = await ExecuteCommand().run(
        {"command": "echo WARNING >&2; echo fine", "timeout": 30}, local_context())
    assert res.ok is True
    assert (res.output or {}).get("stderr_on_success") is True


@pytest.mark.asyncio
async def test_a_quiet_success_is_not_flagged() -> None:
    res = await ExecuteCommand().run({"command": "echo fine", "timeout": 30}, local_context())
    assert "stderr_on_success" not in (res.output or {})


# ── output must not lie about being complete ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_truncated_output_says_so() -> None:
    """Cutting at the cap with no marker turns a partial log into what reads as a whole one."""
    res = await ExecuteCommand().run(
        {"command": "head -c 400000 /dev/zero | tr '\\0' 'x'", "timeout": 60}, local_context())
    assert (res.output or {}).get("truncated") is True
    assert "truncated" in (res.output or {}).get("stdout", "")


# ── the sandbox must never be silently downgraded ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sandbox_and_background_are_refused_not_downgraded(tmp_path) -> None:
    """background used to win silently: the command ran on the real host, unsandboxed, and verify()
    blessed it. Dropping the stronger guarantee without saying so is the worst option available."""
    marker = tmp_path / "SHOULD_NOT_EXIST"
    res = await ExecuteCommand().run(
        {"command": f"touch {marker}", "sandbox": True, "background": True, "timeout": 30},
        local_context())
    assert res.ok is False
    assert "cannot be combined" in (res.error or "")
    assert not os.path.exists(marker), "the command ran despite being refused"
