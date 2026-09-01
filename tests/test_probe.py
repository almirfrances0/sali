"""Verification composition (§8/§23): post-condition probes RE-OBSERVE reality, the command-shape
router picks the right one, and ExecuteCommand.verify() uses it — so an effect is verified, not assumed."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from sali.tools.builtins.exec_tool import ExecuteCommand
from sali.tools.context import local_context
from sali.tools.probe import (
    binary_available,
    path_absent,
    path_exists,
    verify_command,
)
from sali.verify.engine import verify_effect


def test_path_probes_reobserve_reality() -> None:
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, "made.txt")
        r1 = path_exists(f)
        assert r1 is not None and r1.success is False  # not there yet
        Path(f).write_text("x")
        r2 = path_exists(f)
        assert r2 is not None and r2.success is True
        r3 = path_absent(f)
        assert r3 is not None and r3.success is False
    # a relative path or a glob → no opinion (None), never a fabricated verdict
    assert path_exists("relative/path") is None
    assert path_exists("/tmp/*.log") is None


async def test_router_dispatches_by_command_shape() -> None:
    with tempfile.TemporaryDirectory() as d:
        made = os.path.join(d, "sub")
        Path(made).mkdir()
        v = await verify_command(f"mkdir -p {made}")
        assert v is not None and v.success  # mkdir → verify the dir exists

    absent = "/tmp/sali-definitely-not-here-949231"
    v = await verify_command(f"rm -f {absent}")
    assert v is not None and v.success  # rm → verify the path is gone

    # a plain read has no effect to verify → None (caller falls back to the exit code)
    assert await verify_command("ls -la /") is None
    assert await verify_command("grep foo bar.txt") is None


async def test_binary_probe_when_available() -> None:
    v = await binary_available("sh")  # sh is on PATH everywhere the suite runs
    if v is not None:  # None only if `which` itself is missing — then we simply have no opinion
        assert v.success
    missing = await binary_available("sali-no-such-binary-xyz")
    if missing is not None:
        assert not missing.success


async def test_verify_effect_catalog_delegates() -> None:
    # the higher-layer catalog (used by crash-recovery) re-observes the same way
    v = await verify_effect("rm -f /tmp/sali-definitely-not-here-771")
    assert v is not None and v.success
    assert await verify_effect("echo hello") is None  # nothing to independently verify


async def test_execute_command_verify_end_to_end() -> None:
    tool = ExecuteCommand()
    ctx = local_context()
    with tempfile.TemporaryDirectory() as d:
        target = os.path.join(d, "created")
        res = await tool.run({"command": f"mkdir -p {target}"}, ctx)
        assert res.ok
        v = await tool.verify({"command": f"mkdir -p {target}"}, res, ctx)
        assert v.success and "exists" in v.detail  # verified against reality, not the exit code

    # a read with no probe falls back to the exit code — success on 0…
    res = await tool.run({"command": "true"}, ctx)
    assert (await tool.verify({"command": "true"}, res, ctx)).success
    # …and honest failure on non-zero
    res = await tool.run({"command": "false"}, ctx)
    assert not (await tool.verify({"command": "false"}, res, ctx)).success
