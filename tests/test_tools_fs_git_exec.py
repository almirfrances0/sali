"""Phase 5 tools: PathGuard confinement, filesystem, git, and the jailed execute_command."""

from __future__ import annotations

from pathlib import Path

import pytest

from sali.config.settings import PermissionsSettings, Settings
from sali.core.clock import SystemClock
from sali.tools import jail
from sali.tools.builtins.exec_tool import ExecuteCommand
from sali.tools.builtins.filesystem import CreateFile, ListDir, ModifyFile, ReadFile
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
        guard.check_read("/etc/passwd")  # outside the read root
    with pytest.raises(PathViolation):
        guard.check_read(tmp_path / "secret" / "key")  # inside a denied prefix
    with pytest.raises(PathViolation):
        guard.check_read(tmp_path / ".." / "escape")  # .. escape is resolved then rejected
    with pytest.raises(PathViolation):
        guard.check_write(tmp_path / ".git" / "config")  # .git writes are always denied


# ---- filesystem tools -----------------------------------------------------------------
async def test_create_read_modify_within_root(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    created = await CreateFile().run({"path": str(tmp_path / "note.txt"), "content": "hello"}, ctx)
    assert created.ok
    read = await ReadFile().run({"path": str(tmp_path / "note.txt")}, ctx)
    assert read.ok and read.output["content"] == "hello"
    listing = await ListDir().run({"path": str(tmp_path)}, ctx)
    assert any(e["name"] == "note.txt" for e in listing.output["entries"])
    modified = await ModifyFile().run({"path": str(tmp_path / "note.txt"), "content": "bye"}, ctx)
    assert modified.ok


async def test_write_outside_root_is_denied(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = await CreateFile().run({"path": "/tmp/sali_escape.txt", "content": "x"}, ctx)
    assert not result.ok and "denied" in result.display


async def test_read_denied_prefix(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    (tmp_path / "secret").mkdir()
    (tmp_path / "secret" / "key").write_text("s3cr3t")
    result = await ReadFile().run({"path": str(tmp_path / "secret" / "key")}, ctx)
    assert not result.ok and "denied" in result.display


# ---- git --------------------------------------------------------------------------------
async def test_git_status_on_project_repo() -> None:
    # The Sali project is a git repo (default read roots include the home dir).
    result = await GitStatus().run({"repo": str(Path(__file__).resolve().parent.parent)}, local_context())
    assert result.ok
    assert "stdout" in result.output


# ---- execute_command (jailed) -----------------------------------------------------------
async def test_execute_allowlisted_binary_runs_jailed() -> None:
    ctx = local_context()  # default allowlist includes uname; jail on
    result = await ExecuteCommand().run({"command": ["uname", "-s"]}, ctx)
    assert result.ok
    assert "Linux" in result.output["stdout"]


async def test_execute_rejects_non_allowlisted() -> None:
    result = await ExecuteCommand().run({"command": ["rm", "-rf", "/"]}, local_context())
    assert not result.ok and "allowlist" in (result.error or "")


async def test_execute_refuses_interpreters_even_if_allowlisted() -> None:
    perms = PermissionsSettings(exec_allowlist=["python3"])  # deliberately allow the interpreter
    ctx = ToolContext(settings=Settings(permissions=perms), clock=SystemClock())
    result = await ExecuteCommand().run({"command": ["python3", "-c", "print(1)"]}, ctx)
    assert not result.ok and "interpreter" in (result.error or "")


async def test_execute_refuses_when_jail_required_but_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jail, "available", lambda: False)
    result = await ExecuteCommand().run({"command": ["uname"]}, local_context())
    assert not result.ok and "sandbox" in (result.error or "")


async def test_git_is_not_runnable_via_execute_command() -> None:
    # git was removed from the exec allowlist (it is a code-execution engine via -c alias=!sh).
    result = await ExecuteCommand().run(
        {"command": ["git", "-c", "alias.x=!id", "x"]}, local_context()
    )
    assert not result.ok and "allowlist" in (result.error or "")


async def test_execute_rejects_path_in_argv0() -> None:
    # A path lets a planted binary with an allowlisted basename run — bare names only.
    result = await ExecuteCommand().run({"command": ["/usr/bin/uname"]}, local_context())
    assert not result.ok and "bare binary name" in (result.error or "")


async def test_execute_cannot_reach_secrets_outside_cwd() -> None:
    # The jail binds only exec_cwd, so a home path (e.g. ~/.ssh) is simply not present inside it.
    result = await ExecuteCommand().run({"command": ["stat", "/home/almir/.ssh"]}, local_context())
    assert not result.ok  # stat fails: the path does not exist in the sandbox
