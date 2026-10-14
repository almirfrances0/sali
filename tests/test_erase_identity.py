"""The erase must reset what Sali KNOWS without breaking how the app FINDS him.

Both defects were live on this machine. The 18:09 erase orphaned conversation
01a078a0-c67e-75b7-929a-603feaff9c00 with 14 real messages in it — replies Sali sent that the app
could never display, because the API had already minted a new conversation id from a file the reset
had deleted underneath the running process.

The 2026-10-14 erase deleted `~/.local/share/sali/models/` along with the state files, which
caused llama-server to crash-loop 175 times because the 17.5 GB GGUF weights were gone — the
daemon could no longer think at all. A reset wipes memory + archives, never the binaries that
ARE Sali's brain.
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


def test_the_models_never_burn(tmp_path, monkeypatch) -> None:
    """A memory reset wipes what Sali KNOWS, never the binaries that are his brain.

    Regression for the 2026-10-14 incident: `_clear_state_files` iterated every entry in
    `~/.local/share/sali/` and `shutil.rmtree`'d anything not in STATE_PRESERVE. `models/`
    was not preserved, so the reset deleted the 17.5 GB Qwen3.5 GGUF, the mmproj vision
    projector, and the embedder. llama-server then crash-looped 175 times and Sali could not
    answer a single message. A reset is allowed to erase his memories, never his mind."""
    state = tmp_path / "sali"
    state.mkdir()
    (state / "session").write_text("01a078c4-99ed-7122-ba5e-e383817b1cb2")
    (state / "history").write_text("some cli history")
    models = state / "models"
    models.mkdir()
    # Stand-in for the 17.5 GB GGUF; the test asserts the DIRECTORY and its CONTENTS both survive.
    (models / "Qwen3.5-35B-A3B-UD-IQ4_XS.gguf").write_bytes(b"fake gguf bytes")
    (models / "mmproj-BF16.gguf").write_bytes(b"fake mmproj bytes")
    (models / "nomic-embed-text-v1.5.Q8_0.gguf").write_bytes(b"fake embed bytes")

    monkeypatch.setattr(reset_mod, "STATE_DIR", state)
    reset_mod._clear_state_files()

    # models/ and every GGUF inside it must survive — this is the actual failure mode we're hardening.
    assert models.is_dir(), "reset must NEVER delete the model weights directory"
    for name in ("Qwen3.5-35B-A3B-UD-IQ4_XS.gguf", "mmproj-BF16.gguf",
                 "nomic-embed-text-v1.5.Q8_0.gguf"):
        assert (models / name).exists(), f"reset wiped {name} — llama-server cannot load without it"
    # unrelated state is still cleared as expected
    assert not (state / "history").exists(), "CLI history is state and must still be cleared"
    # belt-and-braces: the name is literally in the preserve set
    assert "models" in reset_mod.STATE_PRESERVE


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
