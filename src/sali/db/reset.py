"""Factory reset — put Sali back to the beginning.

Erases everything Sali has learned, done, or remembered, so a test install can become a production one:
memories + the knowledge graph, every conversation and message, the whole event/audit log, all tasks with
their steps/artifacts/reviews, learning + experience + skills, schedules, commitments, initiatives, and
the rest of the work state.

What SURVIVES is only what would otherwise lock the owner out or invalidate the install:
  * ``schema_migrations`` — the schema itself (this is a data reset, never a migration rollback)
  * ``api_device`` / ``device_session`` / ``enrollment_code`` — the enrolled phone stays logged in
  * the login password + the chosen chat model — carried across on the re-seeded ``sali_state`` row
  * ``freshness_rule`` / ``layer_policy`` — seeded policy config, not user data

WHY sali_state is wiped and re-seeded rather than skipped: it holds two foreign keys into ``task``
(active_task_id, previous_task_id). Postgres TRUNCATE ... CASCADE also truncates every table holding an
FK INTO the truncated set — so leaving sali_state out would either abort the whole reset or silently
cascade and destroy the password, locking the owner out of their own app. Saving the auth values and
re-inserting the singleton is the only version of this that is both complete and safe.

On disk, two things are cleared: Sali's own archive directories (ARCHIVE_SUBDIRS) — finished work is
archived OUT of the database, so a DB-only wipe would leave the whole Work history still on screen — and
Sali's state files (STATE_DIR: session identity, background run logs, CLI history).

The owner's actual documents (loose files and project directories in the workspace) are kept, as are the
DB backups and the sudo-askpass helper: this resets what Sali KNOWS and DID, not the things you made
together and not the means of recovering if this reset was a mistake.
"""

from __future__ import annotations

import contextlib
import shutil
from pathlib import Path
from typing import Any

from sali.obs.log import get_logger

log = get_logger("sali.db.reset")

# Tables that must survive a reset (see the module docstring for why each one).
PRESERVED_TABLES = frozenset({
    "schema_migrations",
    "api_device", "device_session", "enrollment_code",
    "freshness_rule", "layer_policy",
})

# Sali's ON-DISK history stores. A DB-only reset is NOT a reset: finished work is archived OUT of the
# database on completion, and the Work screen's History reads those files directly
# (GET /tasks/history walks <workspace>/tasks/*/meta.json). So wiping only the tables left every
# archived task on screen. These four directories are Sali's own bookkeeping — task archives, conversation
# archives, research reports, and the outbox of files sent to the phone.
#
# Everything ELSE in the workspace is deliberately left alone: loose files and project directories are
# the documents produced together, not Sali's history, and destroying those is not what "start over" means.
ARCHIVE_SUBDIRS = ("tasks", "conversations", "research", "sends")

# Sali's state OUTSIDE the workspace. The session id is deliberately included: a production life should
# begin with a fresh conversation identity rather than silently inheriting the one used during testing.
# Two entries are PRESERVED by name — `backups` (a reset must never destroy the safety net that lets you
# recover from a reset) and `sudo-askpass` (an operational helper; deleting it would break sudo).
STATE_DIR = Path.home() / ".local" / "share" / "sali"
STATE_PRESERVE = frozenset({"backups", "sudo-askpass"})


def _clear_state_files() -> list[str]:
    """Clear Sali's on-disk state files (session identity, background run logs, CLI history)."""
    cleared: list[str] = []
    if not STATE_DIR.is_dir():
        return cleared
    for entry in sorted(STATE_DIR.iterdir()):
        if entry.name in STATE_PRESERVE:
            continue
        with contextlib.suppress(Exception):
            if entry.is_dir():
                shutil.rmtree(entry)
                entry.mkdir(parents=True, exist_ok=True)
            else:
                entry.unlink()
            cleared.append(str(entry))
    return cleared


def _clear_archives(workspace: str | None) -> list[str]:
    """Empty Sali's on-disk archive directories. The directory itself is recreated so writers that
    assume it exists keep working. Best-effort per directory — a reset must not half-fail on a
    permission error."""
    cleared: list[str] = []
    if not workspace:
        return cleared
    base = Path(workspace).expanduser()
    if not base.is_dir():
        return cleared
    for name in ARCHIVE_SUBDIRS:
        target = base / name
        if not target.is_dir():
            continue
        with contextlib.suppress(Exception):
            shutil.rmtree(target)
            target.mkdir(parents=True, exist_ok=True)
            cleared.append(str(target))
    return cleared


async def factory_reset(pool: Any, *, workspace: str | None = None) -> dict[str, Any]:
    """Wipe every knowledge/work/history table AND Sali's on-disk archives, keeping the install valid."""
    async with pool.acquire() as conn, conn.transaction():
        saved = await conn.fetchrow(
            "SELECT auth_password_hash, auth_password_set_at, active_chat_model "
            "FROM sali.sali_state WHERE id = true")

        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'sali' AND table_type = 'BASE TABLE'")
        # Everything not explicitly preserved — so a table added in a future migration is wiped by
        # DEFAULT rather than quietly surviving a "reset" and leaking the old life into the new one.
        targets = sorted({r["table_name"] for r in rows} - PRESERVED_TABLES)
        if targets:
            await conn.execute(
                "TRUNCATE " + ", ".join(f"sali.{t}" for t in targets) + " RESTART IDENTITY CASCADE")

        # Re-seed the singleton with ONLY the credentials + model choice carried over. Every other
        # column returns to its schema default: no focus, no last success/failure, turn_count 0.
        await conn.execute(
            "INSERT INTO sali.sali_state (id, auth_password_hash, auth_password_set_at, active_chat_model) "
            "VALUES (true, $1, $2, $3)",
            saved["auth_password_hash"] if saved else None,
            saved["auth_password_set_at"] if saved else None,
            saved["active_chat_model"] if saved else None)

    # The durable archive lives on disk and is what the History screen reads — clearing the tables
    # without this leaves every finished task still listed.
    archives = _clear_archives(workspace)
    state_files = _clear_state_files()

    log.warning("factory_reset_completed", tables_cleared=len(targets),
                archives=archives, state_files=len(state_files))
    return {
        "reset": True,
        "tables_cleared": len(targets),
        "cleared": targets,
        "archives_cleared": archives,
        "state_cleared": state_files,
        "preserved": sorted(PRESERVED_TABLES) + ["sali_state (password + chosen model only)"],
        "note": "Your own files in the workspace were kept — only Sali's archives were cleared.",
    }
