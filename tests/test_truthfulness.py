"""Truthfulness: does Sali check what he is told, and know the difference between the two?

These are BEHAVIOURAL tests, not word tests. Nothing here asserts that a reply contains a phrase; every
assertion is about state Sali actually reached — what the machine reported, what was written to memory,
what was queued to be verified. A test that checked for the string "I checked" would pass on a system
that had checked nothing, which is the exact failure the whole directive is about.

Scenarios from the directive §28: (1) Almir asserts something checkable and false, (2) he asserts a file
that does not exist, (5)/(12) memory conflicts with observation, (19) Sali must be able to say he does
not know, and (22) attempted vs completed vs verified must not collapse.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from sali.core.enums import MemoryLayer, MemorySource
from sali.core.knowledge import looks_checkable
from sali.memory import writer
from sali.verify.claims import check_claims, render

# A name no packaging system will ever have. Used instead of a real absent program so the test does not
# start failing the day someone installs that program on the host.
ABSENT = "zzznotarealprogram7391"


# ── the machine settles what Almir asserts, before the answer ───────────────────────────────────────

async def test_false_claim_about_a_program_is_contradicted_by_the_machine() -> None:
    """§28.1 — Almir says a program is installed and it is not. Sali must hold the observation."""
    checks = await check_claims(f"{ABSENT} is installed")
    assert len(checks) == 1
    assert checks[0].asserted is True          # what he said
    assert checks[0].observed is False         # what the machine says
    assert checks[0].agrees is False           # and Sali knows they differ
    assert ABSENT in render(checks)


async def test_true_claim_about_a_program_is_confirmed_not_merely_accepted() -> None:
    """The other half: agreeing is only worth something if it was checked. `sh` is on every POSIX host."""
    checks = await check_claims("sh is installed")
    assert [(c.asserted, c.observed, c.agrees) for c in checks] == [(True, True, True)]


async def test_a_denial_is_checked_too() -> None:
    """"X isn't installed" is exactly as checkable as "X is installed", and just as wrong when wrong."""
    checks = await check_claims("sh is not installed")
    assert len(checks) == 1
    assert checks[0].asserted is False and checks[0].observed is True and not checks[0].agrees


async def test_false_claim_about_a_file(tmp_path: Path) -> None:
    """§28.2 — Almir says a file exists when it does not."""
    missing = tmp_path / "not_here.txt"
    checks = await check_claims(f"{missing} exists")
    assert len(checks) == 1 and checks[0].observed is False and not checks[0].agrees

    present = tmp_path / "here.txt"
    present.write_text("x")
    checks = await check_claims(f"{present} exists")
    assert len(checks) == 1 and checks[0].observed is True and checks[0].agrees


async def test_the_editor_case_that_started_this(tmp_path: Path) -> None:
    """The real exchange: Almir said which editor he uses, on a machine that could have answered.

    Sali stored it, recalled it for turns, and never once looked. Whatever the host actually has, the
    claim must now be SETTLED rather than accepted — that is the whole property under test, so the
    assertion is on having an observation, not on which way it went."""
    checks = await check_claims("for the record, i use VS Code as my editor on this machine.")
    assert len(checks) == 1
    assert checks[0].observed is (shutil.which("code") is not None)


# ── silence must mean "not checked", never "checked and fine" ───────────────────────────────────────

@pytest.mark.parametrize("text", [
    "how are you today?",
    "can you write me a python script that tars a folder",
    "thanks, that worked",
    "what do you think we should do about the backup situation",
    "i use github for my code",          # a service, not a program on this machine
    "what is installed on this machine?",  # a QUESTION about a subject named "what"
])
async def test_ordinary_conversation_probes_nothing(text: str) -> None:
    """The cost of a false positive here is Sali confidently contradicting a sentence that was fine."""
    assert await check_claims(text) == []


async def test_unknown_service_yields_no_opinion() -> None:
    """`systemctl is-active` answers "inactive" for a unit that does not exist, identically to one that
    is merely stopped. Reporting that would contradict a correct statement about something else, so a
    unit systemd has never heard of must produce silence."""
    assert await check_claims("the zzznosuchunit service is running") == []


async def test_a_generic_subject_is_not_a_unit_name() -> None:
    """"the daemon is running" is a sentence about something, not about a unit called `daemon`."""
    assert await check_claims("the daemon is running") == []


async def test_a_question_is_answered_but_never_contradicted() -> None:
    """Looking is still better than guessing (§11), but there was no claim to disagree with."""
    checks = await check_claims("is /etc/hostname there?")
    for check in checks:
        assert check.is_question and check.agrees


# ── being told and having looked are different things ───────────────────────────────────────────────

@pytest.mark.db
async def test_what_almir_says_is_believed_and_still_queued_to_be_checked(db_conn: Any) -> None:
    """The conflation at the root of §2: a user statement scored above the promotion bar, so it was
    trusted instantly and nothing ever went to look. It must now be BOTH — believed, and queued."""
    memory = await writer.remember(
        db_conn, layer=MemoryLayer.SEMANTIC,
        content="You use VS Code as your primary text editor on this machine.",
        source=MemorySource.USER_EXPLICIT,
    )
    row = await db_conn.fetchrow(
        "SELECT confidence, needs_grounding FROM memory WHERE id=$1", memory.id)
    assert row["confidence"] > 0.55, "Almir is usually right; his word is still believed"
    assert row["needs_grounding"] is True, "and it is still going to be checked"


@pytest.mark.db
async def test_an_observation_is_not_queued_because_it_already_is_the_look(db_conn: Any) -> None:
    memory = await writer.remember(
        db_conn, layer=MemoryLayer.SYSTEM_ENV,
        content="This machine has 12GB of VRAM and docker is installed.",
        source=MemorySource.SYSTEM_OBSERVATION,
    )
    assert await db_conn.fetchval(
        "SELECT needs_grounding FROM memory WHERE id=$1", memory.id) is False


@pytest.mark.db
async def test_nothing_the_machine_cannot_settle_is_queued(db_conn: Any) -> None:
    """A taste is not falsifiable by looking at a filesystem, and flagging it would make Sali hedge
    about the one kind of thing Almir is the sole authority on."""
    memory = await writer.remember(
        db_conn, layer=MemoryLayer.PREFERENCE, content="Almir prefers short answers.",
        source=MemorySource.USER_EXPLICIT,
    )
    assert await db_conn.fetchval(
        "SELECT needs_grounding FROM memory WHERE id=$1", memory.id) is False


@pytest.mark.db
async def test_identity_is_never_queued_for_verification(db_conn: Any) -> None:
    """Nothing on the machine can confirm who Sali is, so asking it to would queue a question that can
    never be answered and would block the grounding worklist behind it forever."""
    memory = await writer.remember(
        db_conn, layer=MemoryLayer.IDENTITY,
        content="Sali runs on this machine and persists across restarts.",
        source=MemorySource.USER_EXPLICIT,
    )
    assert await db_conn.fetchval(
        "SELECT needs_grounding FROM memory WHERE id=$1", memory.id) is False


def test_the_definition_of_checkable_covers_the_claim_it_used_to_miss() -> None:
    """"your preferred code editor is Neovim" was stored, recalled and repeated for turns while `nvim`
    was not on the machine — because the pattern had no idea an editor was a checkable thing."""
    assert looks_checkable("your preferred code editor is Neovim now")
    assert looks_checkable("You use VS Code as your primary text editor on this machine.")
    assert looks_checkable("docker is installed")
    assert not looks_checkable("Almir prefers short answers")
    assert not looks_checkable("that conversation went well")


# ── the self-state singleton must survive a wipe ────────────────────────────────────────────────────

@pytest.mark.db
async def test_self_state_writes_survive_a_deleted_singleton(db_conn: Any) -> None:
    """`sali_state` has foreign keys to `task`, so a TRUNCATE task CASCADE — what a "clear everything
    from testing" sweep runs — deletes the singleton. The writes were plain UPDATEs, which then matched
    nothing and reported success: Sali silently stopped recording what he was doing. Measured on the
    live database at the time: 0 rows in sali_state against 161 presence events."""
    from sali.runtime.self_state import SelfStateStore

    await db_conn.execute("DELETE FROM sali_state")

    class _OneConnPool:
        def acquire(self) -> Any:
            conn = db_conn

            class _Ctx:
                async def __aenter__(self) -> Any:
                    return conn

                async def __aexit__(self, *exc: Any) -> bool:
                    return False

            return _Ctx()

    store = SelfStateStore(_OneConnPool())
    await store.note_turn("checking the backup")
    await store.record_outcome(success=False, summary="the backup script exited 1")

    row = await db_conn.fetchrow("SELECT mode, turn_count, last_failure, last_failure_at FROM sali_state")
    assert row is not None, "the write must re-create the singleton, not vanish"
    assert row["turn_count"] == 1
    assert row["last_failure"] == "the backup script exited 1"
    assert row["last_failure_at"] is not None, "a failure with no time cannot be judged recent"
