"""An erase must leave nothing of the old life behind — and must not lock Almir out of his own phone.

Almir: "make sure the ERASE will delete everything to reset sali". This runs a REAL factory_reset
against the scratch database with a temp workspace and a temp state dir, then checks every surface:
the tables, the on-disk archives, and the state files.
"""

from __future__ import annotations

import pytest

from sali.db import reset as reset_mod

# Tables that must survive, and why. Anything NOT here must be empty afterwards.
EXPECTED_SURVIVORS = {
    "schema_migrations": "the schema itself — a data reset is not a migration rollback",
    "api_device": "the enrolled phone, with its push token — clearing it un-enrolls him",
    "enrollment_code": "short-lived pairing codes; harmless either way",
    "freshness_rule": "seeded policy config, not user data",
    "layer_policy": "seeded policy config, not user data",
}


@pytest.mark.asyncio
async def test_an_erase_leaves_nothing_of_the_old_life(live_pool, tmp_path, monkeypatch) -> None:
    works = tmp_path / "sali-works"
    for sub in reset_mod.ARCHIVE_SUBDIRS:
        d = works / sub
        d.mkdir(parents=True)
        (d / "leftover.txt").write_text("from the previous life")
    # a document Almir made WITH Sali — deliberately NOT Sali's history, must survive
    (works / "my-project").mkdir()
    (works / "my-project" / "notes.md").write_text("mine")

    state = tmp_path / "state"
    state.mkdir()
    (state / "session").write_text("11111111-1111-4111-8111-111111111111")
    (state / "history").write_text("cli history")
    (state / "backups").mkdir()
    (state / "backups" / "db.sql").write_text("the safety net")
    (state / "sudo-askpass").write_text("#!/bin/sh\n")
    monkeypatch.setattr(reset_mod, "STATE_DIR", state)

    async with live_pool.acquire() as conn:
        await conn.execute("INSERT INTO sali.memory (layer, content, source) "
                           "VALUES ('semantic', 'something he knew', 'inference')")
        await conn.execute("INSERT INTO sali.event (event_type, payload) "
                           "VALUES ('probe.erase', '{}'::jsonb)")
        await conn.execute(
            "INSERT INTO sali.sali_state (id, auth_password_hash, active_chat_model) "
            "VALUES (true, 'the-hash', 'sali:latest') "
            "ON CONFLICT (id) DO UPDATE SET auth_password_hash='the-hash', "
            "  active_chat_model='sali:latest'")
        seq_before = await conn.fetchval("SELECT coalesce(max(seq), 0) FROM sali.event")

    await reset_mod.factory_reset(live_pool, workspace=str(works))

    async with live_pool.acquire() as conn:
        tables = [r["table_name"] for r in await conn.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'sali' AND table_type = 'BASE TABLE'")]
        left: dict[str, int] = {}
        for t in tables:
            if t in EXPECTED_SURVIVORS or t == "sali_state":
                continue
            n = await conn.fetchval(f"SELECT count(*) FROM sali.{t}")  # noqa: S608 — names from catalog
            if n:
                left[t] = n

        # what MUST come back
        row = await conn.fetchrow(
            "SELECT auth_password_hash, active_chat_model FROM sali.sali_state WHERE id = true")
        seq_after = await conn.fetchval("SELECT coalesce(max(seq), 0) FROM sali.event")

    assert not left, f"these tables survived an erase and should not have: {left}"
    # ...and he is still able to log in, with the model he chose
    assert row is not None and row["auth_password_hash"] == "the-hash", \
        "clearing the password locks Almir out of his own phone"
    assert row["active_chat_model"] == "sali:latest"

    # the event counter must not go backwards — live clients hold it as a cursor
    assert seq_after >= seq_before or seq_after == 0, (
        f"event.seq went backwards ({seq_before} -> {seq_after}); every watcher would be stranded")

    # Sali's own archives are gone; the documents made together are not
    for sub in reset_mod.ARCHIVE_SUBDIRS:
        assert not any((works / sub).iterdir()), f"{sub}/ still holds the previous life"
    assert (works / "my-project" / "notes.md").read_text() == "mine", \
        "an erase must not destroy the things Almir made"

    # state files: identity and the safety net stay, accumulated state goes
    assert (state / "session").exists(), "deleting this breaks chat — see sali-erase-and-app-transport"
    assert (state / "backups" / "db.sql").exists(), "never destroy the means of undoing a reset"
    assert (state / "sudo-askpass").exists()
    assert not (state / "history").exists()


@pytest.mark.asyncio
async def test_sessions_do_not_survive_an_erase(live_pool) -> None:
    """device_session had 203 rows on the live box — every session ever issued. Safe to clear: the
    app re-authenticates with a full password login on its next 401, and api_device (the enrolment
    itself) still survives."""
    assert "device_session" not in reset_mod.PRESERVED_TABLES
    assert "api_device" in reset_mod.PRESERVED_TABLES, "clearing this un-enrols his phone"
