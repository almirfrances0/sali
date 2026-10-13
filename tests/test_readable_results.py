"""Four things Sali could not read correctly, and now can."""
from __future__ import annotations

from sali.tools.builtins.exec_tool import _exit_meaning


def test_127_is_not_a_crash() -> None:
    """Sali reported "python3 crashed again (exit 127)". 127 is the shell saying the program was
    never found: nothing ran, so nothing crashed — and "not installed" is a durable fact this machine
    can learn, where "crashed" invites retrying something that can never work."""
    m = _exit_meaning(127)
    assert "not found" in m and "NOT installed" in m
    assert "crash" in m.lower()          # says so explicitly, so it cannot be read the other way


def test_the_codes_that_mean_different_things_read_differently() -> None:
    assert "not executable" in _exit_meaning(126)
    assert "out-of-memory" in _exit_meaning(137)
    assert "segmentation" in _exit_meaning(139)
    assert "SIGINT" in _exit_meaning(130)


def test_a_signal_death_is_named_even_when_unlisted() -> None:
    assert _exit_meaning(152) == "exit 152 (killed by signal 24)"


def test_an_unknown_code_invents_nothing() -> None:
    """Better a bare number than a plausible story about it."""
    assert _exit_meaning(99) == "exit 99"


def test_zero_is_ok() -> None:
    assert _exit_meaning(0) == "ok"


def test_file_metadata_returns_a_readable_date(tmp_path) -> None:
    """`"mtime": 1788774842` was read by a Q2_K model as "mid-June — exactly three months ago". It was
    05:54 that same morning, and the fabricated date then reached durable memory via `remember`."""
    import asyncio

    from sali.config.settings import Settings
    from sali.core.clock import SystemClock
    from sali.tools.builtins.filesystem import FileMetadata
    from sali.tools.context import ToolContext

    f = tmp_path / "x.txt"
    f.write_text("hi")
    ctx = ToolContext(settings=Settings(), clock=SystemClock())
    res = asyncio.run(FileMetadata().run({"path": str(f)}, ctx))
    assert res.ok, res.error
    assert "UTC" in res.output["modified"]
    assert res.output["age"]                       # no subtraction left for the model to get wrong
    assert isinstance(res.output["mtime"], int)    # the epoch survives for callers that sort on it
    assert "modified" in res.display
