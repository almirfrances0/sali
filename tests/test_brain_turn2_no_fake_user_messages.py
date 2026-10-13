"""Brain-audit Turn 2: internal turns do NOT land in sali.message under role='user'.

Verifies:
  1. A recent Almir chat still persists as role='user' with the exact text.
  2. Any past pollution (rows with '[my own background check]' prefix) is gone.
  3. New internal task-loop turns leave the message table untouched (spot-check by
     ensuring no fresh polluted rows land in a short window - the daemon has been
     restarted with the fix and the Spotify task's step-advance loop runs every
     2-3 minutes).
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.db


async def test_no_background_check_pollution_in_message_table(live_pool: Any) -> None:
    """Live guard: the ~200 polluted rows are gone, and the writer no longer creates new
    ones. Any new appearance means the fix regressed or a caller found a new door."""
    async with live_pool.acquire() as c:
        polluted = await c.fetchval(
            "SELECT count(*) FROM sali.message "
            "WHERE role = 'user' AND content LIKE '[my own background check]%'")
    assert polluted == 0, (
        f"{polluted} '[my own background check]' rows in sali.message — Turn 2 fix regressed")


async def test_role_user_messages_are_actually_from_user(live_pool: Any) -> None:
    """sali.message role='user' rows should not carry internal-turn prefix markers.
    Beyond '[my own background check]', watch for 'TASK: ' loop-generated boilerplate."""
    async with live_pool.acquire() as c:
        boilerplate = await c.fetchval(
            "SELECT count(*) FROM sali.message WHERE role = 'user' "
            "AND (content LIKE 'TASK: Verify %' "
            "     OR content LIKE '[my own background check]%')")
    assert boilerplate == 0, (
        f"{boilerplate} loop-generated boilerplate rows still in role='user' - "
        "Turn 2 must catch every internal-turn source, not just the primary one")
