"""§60 end-to-end: a goal-shaped statement from Almir lands on the iPhone.

Turn 1 shipped the AgendaSynthesiser (read-model across six stores). Turn 2 shipped the
goal-capture-from-chat hook. Turn 3 shipped the iOS Agenda screen that reads /api/v1/agenda.
The three pieces have unit tests; this file proves they COMPOSE - that a real user_input
walks the whole path and surfaces in the same synthesised view the phone will render.

Uses `live_pool` (real Postgres, real GoalStore, real AgendaSynthesiser) with the model
stubbed out via `FakeModelProvider` - the only piece that would require an LLM to run.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sali.core.temporal import TemporalService
from sali.provider.base import ChatResult
from sali.provider.fake import FakeModelProvider
from sali.runtime.loop import AgentLoop
from sali.tasks.agenda import AgendaSynthesiser


class _NoopPublisher:
    async def publish(self, *a, **kw) -> None:
        pass


class _NoopJournal:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def event(self, name: str, payload: dict) -> None:
        self.events.append((name, payload))


def _reply(content: str) -> ChatResult:
    return ChatResult(content=content, thinking=None, tool_calls=[],
                      tokens_in=0, tokens_out=0, model="fake")


async def _capture(pool, provider, user_input) -> _NoopJournal:
    """A `self` shim exposing exactly the three attributes _capture_goal reads."""
    j = _NoopJournal()
    shim = SimpleNamespace(pool=pool, provider=provider, _publisher=_NoopPublisher())
    await AgentLoop._capture_goal(shim, user_input, j)
    return j


async def _clear_goals(pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE sali.goal CASCADE")
        await conn.execute("TRUNCATE sali.task CASCADE")
        await conn.execute("TRUNCATE sali.commitment CASCADE")
        await conn.execute("TRUNCATE sali.initiative CASCADE")


@pytest.mark.asyncio
async def test_goal_from_chat_appears_in_agenda(live_pool) -> None:
    """The full loop: Almir says a goal-shaped thing → goal row → synthesiser sees it."""
    await _clear_goals(live_pool)
    provider = FakeModelProvider(responses=[_reply("GOAL: deploy Salieno on Kali production")])
    j = await _capture(live_pool, provider,
                       "let's deploy Salieno on Kali production this quarter")

    # The capture emitted its event.
    assert any(name == "goal_captured" for name, _ in j.events), j.events

    # The synthesiser (what the model AND /api/v1/agenda both read) now surfaces it.
    view = await AgendaSynthesiser(live_pool, TemporalService()).synthesise()
    payload = view.to_json()
    titles = [g["title"] for g in payload["goals"]]
    assert any("Salieno" in t for t in titles), (
        f"expected goal in synthesised view, got: {payload['goals']}")


@pytest.mark.asyncio
async def test_second_similar_goal_is_suppressed(live_pool) -> None:
    """Follow-up conversation about the same objective must not create a duplicate row.

    Idempotency lives in _capture_goal via keyword-overlap; this proves the overlap check
    reaches into the REAL GoalStore.active() results (not a mocked list)."""
    await _clear_goals(live_pool)
    provider = FakeModelProvider(responses=[
        _reply("GOAL: deploy Salieno on Kali production"),
        _reply("GOAL: let's deploy Salieno to our Kali box"),
    ])
    await _capture(live_pool, provider, "let's deploy Salieno on Kali production")
    j2 = await _capture(live_pool, provider, "let's deploy Salieno on our Kali box")

    async with live_pool.acquire() as conn:
        n = await conn.fetchval("SELECT count(*) FROM sali.goal")
    assert n == 1, f"expected idempotent capture, got {n} goals"
    assert all(name != "goal_captured" for name, _ in j2.events), (
        "second capture must be silent")


@pytest.mark.asyncio
async def test_none_reply_writes_nothing(live_pool) -> None:
    """If the model returns NONE, no row lands even though the gate matched."""
    await _clear_goals(live_pool)
    provider = FakeModelProvider(responses=[_reply("NONE")])
    j = await _capture(live_pool, provider, "let's build a metrics dashboard")

    async with live_pool.acquire() as conn:
        n = await conn.fetchval("SELECT count(*) FROM sali.goal")
    assert n == 0
    assert all(name != "goal_captured" for name, _ in j.events)


@pytest.mark.asyncio
async def test_agenda_integrates_goal_task_and_overdue_commitment(live_pool) -> None:
    """The synthesised view carries a goal alongside a running task in `now` and an overdue
    commitment - proof the iPhone (via /api/v1/agenda) and the model (via context) see the
    exact same cross-store picture in one payload."""
    await _clear_goals(live_pool)

    # (1) Goal via the real capture path.
    provider = FakeModelProvider(responses=[_reply("GOAL: deploy Salieno on Kali production")])
    await _capture(live_pool, provider, "let's deploy Salieno on Kali production")

    # (2) A primary running task → lands in `now`.
    now = datetime.now(timezone.utc)
    async with live_pool.acquire() as conn:
        started = now - timedelta(minutes=15)
        await conn.execute(
            "INSERT INTO sali.task (id, objective, status, is_primary, "
            "  created_at, started_at, updated_at) "
            "VALUES ($1, $2, 'running', TRUE, $3, $3, $3)",
            uuid4(), "Review the ingest pipeline", started)
        # (3) An overdue commitment → lands in `overdue`.
        await conn.execute(
            "INSERT INTO sali.commitment (id, description, status, deadline, updated_at) "
            "VALUES ($1, $2, 'open', $3, $4)",
            uuid4(), "Ping Almir with the fix", now - timedelta(hours=2), now)

    view = await AgendaSynthesiser(live_pool, TemporalService()).synthesise()
    payload = view.to_json()

    now_titles = [i["title"] for i in payload["now"]]
    overdue_titles = [i["title"] for i in payload["overdue"]]
    goal_titles = [g["title"] for g in payload["goals"]]

    assert any("Review the ingest pipeline" in t for t in now_titles), payload
    assert any("Ping Almir" in t for t in overdue_titles), payload
    assert any("Salieno" in t for t in goal_titles), payload


@pytest.mark.asyncio
async def test_agenda_render_is_non_empty_after_capture(live_pool) -> None:
    """The context-side render() the model actually reads is also non-empty end-to-end."""
    await _clear_goals(live_pool)
    provider = FakeModelProvider(responses=[_reply("GOAL: automate the daily deployment")])
    await _capture(live_pool, provider, "long-term I want to automate the daily deployment")

    from sali.tasks.agenda import render
    view = await AgendaSynthesiser(live_pool, TemporalService()).synthesise()
    text = render(view)
    assert text is not None, "render() returned None even though a goal exists"
    assert "Active goals" in text
    assert "automate the daily deployment" in text
