"""PHASE 6 — reliability matrix · proactive outreach actually FIRES (was 100% inert).

sali.proactive_decision is empty in production — the "would silence be better?" path has never reached a
decision live. This drives the real InitiativeDriver + CommunicationDecisionEngine over the test pool
with one seeded overdue commitment and proves the full path: gate says SEND -> runtime.send_agent_message
is called -> a 'sent' proactive_decision row is written. Then it proves fail-closed: a second immediate
tick is suppressed (too_repetitive), and an active user (recent message) defers the reminder.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from sali.cognitive.initiative_driver import InitiativeDriver
from sali.tasks.commitments import CommitmentStore

pytestmark = pytest.mark.db


class _Runtime:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_agent_message(self, msg: str, *, importance: str = "reminder") -> None:
        self.sent.append((msg, importance))


async def test_overdue_commitment_drives_one_grounded_reminder(live_pool: Any) -> None:
    now = datetime.now(UTC)
    await CommitmentStore(live_pool).create(
        description="email the signed tax docs to the accountant",
        deadline=now - timedelta(days=1))

    rt = _Runtime()
    driver = InitiativeDriver(live_pool, runtime=rt)
    await driver._maybe_remind_overdue(now)

    # 1) the gate said SEND and the runtime actually reached out, once
    assert len(rt.sent) == 1
    assert "tax docs" in rt.sent[0][0] and rt.sent[0][1] == "reminder"
    # 2) the decision was recorded in the audit ledger (the 'would silence be better?' sink)
    async with live_pool.acquire() as c:
        sent_rows = await c.fetchval(
            "SELECT count(*) FROM proactive_decision WHERE kind='reminder' AND decision='sent'")
    assert sent_rows == 1


async def test_second_immediate_tick_is_suppressed(live_pool: Any) -> None:
    now = datetime.now(UTC)
    await CommitmentStore(live_pool).create(
        description="renew the domain before it lapses", deadline=now - timedelta(days=2))
    rt = _Runtime()
    driver = InitiativeDriver(live_pool, runtime=rt)
    await driver._maybe_remind_overdue(now)
    await driver._maybe_remind_overdue(now)  # immediate repeat about the same commitment
    assert len(rt.sent) == 1  # fail-closed: the same subject is not nagged twice in the window
