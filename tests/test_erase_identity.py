"""The erase must reset what Sali KNOWS without breaking how the app FINDS him.

Both defects were live on this machine. The 18:09 erase orphaned conversation
01a078a0-c67e-75b7-929a-603feaff9c00 with 14 real messages in it — replies Sali sent that the app
could never display, because the API had already minted a new conversation id from a file the reset
had deleted underneath the running process.
"""

from __future__ import annotations

import pytest

from sali.db import reset as reset_mod


def test_the_conversation_identity_survives_an_erase(tmp_path, monkeypatch) -> None:
    """`session` holds the conversation id. GET /conversation re-reads it per request while the
    runtime holds its own from boot, so deleting it makes the two halves disagree forever: empty
    transcript, replies invisible on reload, live streaming filtered out."""
    state = tmp_path / "sali"
    state.mkdir()
    (state / "session").write_text("01a078c4-99ed-7122-ba5e-e383817b1cb2")
    (state / "history").write_text("some cli history")
    (state / "bg").mkdir()
    (state / "bg" / "run.log").write_text("a background run log")
    (state / "backups").mkdir()
    (state / "backups" / "db.sql").write_text("the safety net")
    (state / "sudo-askpass").write_text("#!/bin/sh\n")

    monkeypatch.setattr(reset_mod, "STATE_DIR", state)
    reset_mod._clear_state_files()

    # kept: identity, the means of recovery, and the sudo helper
    assert (state / "session").read_text() == "01a078c4-99ed-7122-ba5e-e383817b1cb2"
    assert (state / "backups" / "db.sql").exists(), "never destroy the means of undoing a reset"
    assert (state / "sudo-askpass").exists(), "deleting this breaks sudo"
    # cleared: everything that is actually Sali's accumulated state
    assert not (state / "history").exists(), "CLI history is state and must be cleared"
    assert not any((state / "bg").iterdir()), "background run logs are state, not identity"


def test_the_reset_does_not_restart_the_event_sequence() -> None:
    """event.seq is a CURSOR held by live clients — the phone's persisted lastSequence and the
    server's own EventBridge watermark both only ever rise. Restarting at 1 leaves every watcher
    pointing past the end: WS replay returns nothing forever, and the bridge silently stops
    delivering cross-process events until a daemon restart."""
    import inspect
    # Ignore comments — the fix DESCRIBES the thing it removes, so a naive grep matches its own
    # explanation and passes/fails for the wrong reason.
    code = "\n".join(line for line in inspect.getsource(reset_mod).splitlines()
                      if not line.lstrip().startswith("#"))
    assert "RESTART IDENTITY" not in code, (
        "TRUNCATE ... RESTART IDENTITY rolls event.seq back to 1 under every live cursor")


@pytest.mark.asyncio
async def test_a_truncate_without_restart_identity_keeps_the_counter(live_pool) -> None:
    """The behaviour the fix depends on, asserted against a real Postgres rather than assumed."""
    async with live_pool.acquire() as conn:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS _seqprobe (id bigserial primary key, v int)")
        await conn.execute("TRUNCATE _seqprobe CASCADE")
        await conn.execute("INSERT INTO _seqprobe (v) VALUES (1), (2), (3)")
        before = await conn.fetchval("SELECT max(id) FROM _seqprobe")
        await conn.execute("TRUNCATE _seqprobe CASCADE")          # the form the reset now uses
        await conn.execute("INSERT INTO _seqprobe (v) VALUES (9)")
        after = await conn.fetchval("SELECT max(id) FROM _seqprobe")
        await conn.execute("DROP TABLE _seqprobe")
    assert after > before, (
        f"the counter went backwards ({before} -> {after}); every client cursor would be stranded")
