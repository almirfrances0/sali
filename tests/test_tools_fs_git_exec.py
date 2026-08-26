"""Phase 5 tools under the 'Sali's home' model: free execution, self-escalating destruction,
PathGuard confinement, and delete confirmation."""

from __future__ import annotations

from pathlib import Path

import pytest

from sali.config.settings import PermissionsSettings, Settings
from sali.core.clock import SystemClock
from sali.core.enums import RiskLevel
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


def test_delete_self_escalates_outside_scratch() -> None:
    # Scratch deletions run free; anything else escalates to R4 (confirm).
    assert DeleteFile().assess({"path": "/tmp/junk"}) is RiskLevel.R1
    assert DeleteFile().assess({"path": str(Path.home() / "important.txt")}) is RiskLevel.R4


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


async def test_execute_sandbox_mode_isolates() -> None:
    result = await ExecuteCommand().run({"command": "echo sandboxed", "sandbox": True}, local_context())
    assert result.ok and result.output["sandboxed"] is True
    assert "sandboxed" in result.output["stdout"]
