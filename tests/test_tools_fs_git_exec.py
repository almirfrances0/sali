"""Phase 5 tools under the 'Sali's home' model: free execution, self-escalating destruction,
PathGuard confinement, and delete confirmation."""

from __future__ import annotations

from pathlib import Path

import pytest

from sali.config.settings import PermissionsSettings, Settings
from sali.core.clock import SystemClock
from sali.core.enums import RiskLevel
from sali.tools import jail
from sali.tools.builtins.exec_tool import ExecuteCommand
from sali.tools.builtins.filesystem import CreateFile, DeleteFile, ListDir, ModifyFile, ReadFile
from sali.tools.builtins.git import GitStatus
from sali.tools.context import ToolContext, local_context
from sali.tools.pathguard import PathGuard, PathViolation


def _ctx(tmp: Path) -> ToolContext:
    perms = PermissionsSettings(
        fs_read_roots=[str(tmp)], fs_write_roots=[str(tmp)], fs_deny=[str(tmp / "secret")]
    )
    return ToolContext(settings=Settings(permissions=perms), clock=SystemClock())


# ---- PathGuard ------------------------------------------------------------------------
def test_pathguard_confines_and_denies(tmp_path: Path) -> None:
    guard = PathGuard(
        PermissionsSettings(
            fs_read_roots=[str(tmp_path)], fs_write_roots=[str(tmp_path)],
            fs_deny=[str(tmp_path / "secret")],
        )
    )
    assert guard.check_read(tmp_path / "ok.txt") == (tmp_path / "ok.txt").resolve()
    with pytest.raises(PathViolation):
        guard.check_read("/root/anything")  # outside the read root
    with pytest.raises(PathViolation):
        guard.check_read(tmp_path / "secret" / "key")  # inside a denied prefix
    with pytest.raises(PathViolation):
        guard.check_read(tmp_path / ".." / "escape")  # .. escape is resolved then rejected
    # But writing inside a repo IS allowed now (Sali manages its own repos).
    assert guard.check_write(tmp_path / "repo" / ".git" / "config")


# ---- filesystem tools -----------------------------------------------------------------
async def test_create_read_modify_delete_within_root(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    assert (await CreateFile().run({"path": str(tmp_path / "n.txt"), "content": "hi"}, ctx)).ok
    read = await ReadFile().run({"path": str(tmp_path / "n.txt")}, ctx)
    assert read.ok and read.output["content"] == "hi"
    assert any(e["name"] == "n.txt" for e in (await ListDir().run({"path": str(tmp_path)}, ctx)).output["entries"])
    assert (await ModifyFile().run({"path": str(tmp_path / "n.txt"), "content": "bye"}, ctx)).ok
    deleted = await DeleteFile().run({"path": str(tmp_path / "n.txt")}, ctx)
    assert deleted.ok and not (tmp_path / "n.txt").exists()


async def test_write_outside_root_is_denied(tmp_path: Path) -> None:
    result = await CreateFile().run({"path": "/root/escape.txt", "content": "x"}, _ctx(tmp_path))
    assert not result.ok and "denied" in result.display


def test_create_and_modify_escalate_outside_home() -> None:
    # Sali can create a file anywhere, but writing out in the system pauses to confirm (R4);
    # inside its own home (or scratch) it just writes (R1). One rule, not a folder allowlist.
    assert CreateFile().assess({"path": str(Path.home() / "notes" / "todo.md")}) is RiskLevel.R1
    assert CreateFile().assess({"path": "/tmp/scratch.txt"}) is RiskLevel.R1
    assert CreateFile().assess({"path": "/etc/whatever.conf"}) is RiskLevel.R4
    assert CreateFile().assess({"path": "/opt/app/config.yaml"}) is RiskLevel.R4
    assert ModifyFile().assess({"path": str(Path.home() / "a.txt")}) is RiskLevel.R1
    assert ModifyFile().assess({"path": "/usr/local/bin/thing"}) is RiskLevel.R4


def test_delete_self_escalates_outside_scratch() -> None:
    # Scratch deletions run free; anything else escalates to R4 (confirm).
    assert DeleteFile().assess({"path": "/tmp/junk"}) is RiskLevel.R1
    assert DeleteFile().assess({"path": str(Path.home() / "important.txt")}) is RiskLevel.R4
    # Path traversal can't disguise a home file as scratch to skip the confirm (security fix).
    assert DeleteFile().assess({"path": "/tmp/../home/almir/important.txt"}) is RiskLevel.R4


# ---- git --------------------------------------------------------------------------------
async def test_git_status_on_project_repo() -> None:
    result = await GitStatus().run({"repo": str(Path(__file__).resolve().parent.parent)}, local_context())
    assert result.ok and "stdout" in result.output


# ---- execute_command (free, self-escalating) --------------------------------------------
async def test_execute_runs_freely_on_host() -> None:
    result = await ExecuteCommand().run({"command": "echo hello-from-sali"}, local_context())
    assert result.ok
    assert "hello-from-sali" in result.output["stdout"]


async def test_execute_accepts_argv_list() -> None:
    result = await ExecuteCommand().run({"command": ["echo", "hi"]}, local_context())
    assert result.ok and "hi" in result.output["stdout"]


def test_execute_destructive_commands_self_escalate() -> None:
    tool = ExecuteCommand()
    assert tool.assess({"command": "ls -la /home"}) is RiskLevel.R1  # ordinary → free
    assert tool.assess({"command": "rm -rf /home/almir/stuff"}) is RiskLevel.R4  # → confirm
    assert tool.assess({"command": "sudo mkfs.ext4 /dev/sda1"}) is RiskLevel.R4
    assert tool.assess({"command": "dd if=/dev/zero of=/dev/sda"}) is RiskLevel.R4
    assert tool.assess({"command": "apt-get install ripgrep"}) is RiskLevel.R1  # installs are free


def test_sandbox_argv_is_fake_root_ephemeral_and_gates_network() -> None:
    # Pure builder check (no bwrap needed): fake root, ephemeral system overlays, home unmounted.
    argv = jail.build_sandbox_argv(["apt-get", "install", "-y", "cowsay"])
    assert argv[0] == "bwrap"
    assert argv[-5:] == ["--", "apt-get", "install", "-y", "cowsay"]  # command passed through
    joined = " ".join(argv)
    assert "--unshare-user --uid 0 --gid 0" in joined  # fake root → can install/delete
    assert "--tmp-overlay /usr" in joined  # writable but discarded on exit
    assert "--overlay-src" in joined  # never a real bind of the system
    assert "/home" not in joined  # Sali's real files are not mounted in
    assert "--unshare-net" not in argv  # network on by default (installs need it)
    assert "--unshare-net" in jail.build_sandbox_argv(["true"], allow_network=False)


async def test_sandbox_is_fake_root_and_ephemeral() -> None:
    if not jail.available():
        pytest.skip("bubblewrap not available")
    result = await ExecuteCommand().run(
        {"command": "id -u; touch /usr/SANDBOX_TESTX && echo made-in-sandbox", "sandbox": True},
        local_context(),
    )
    assert result.ok and result.output["sandboxed"] is True
    assert "0" in result.output["stdout"]  # fake root inside the sandbox (can install/delete)
    assert "made-in-sandbox" in result.output["stdout"]
    assert not Path("/usr/SANDBOX_TESTX").exists()  # discarded on exit — the host is untouched
