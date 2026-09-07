"""Test fixtures.

Database tests run against the ephemeral ``sali_test`` database on the live cluster with
rollback-per-test isolation, and are auto-skipped when Postgres is unreachable (so
``make ci`` stays green before the one-time bootstrap). No test ever touches Ollama.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from typing import Any

import asyncpg
import pytest

from sali.config.settings import DbSettings, ModelSettings, Settings
from sali.provider.fake import FakeModelProvider


@pytest.fixture(scope="session", autouse=True)
def _isolate_task_archive(tmp_path_factory: Any) -> Iterator[None]:
    """Keep the suite out of Almir's real sali-works archive.

    Any test that creates a task writes <works>/tasks/<id>/meta.json through
    `sali.tasks.logger`. Without this the suite has been permanently littering his workspace — and
    those folders are what the app's History screen reads."""
    import os
    root = tmp_path_factory.mktemp("sali-works-test")
    previous = os.environ.get("SALI_WORKS_ROOT")
    os.environ["SALI_WORKS_ROOT"] = str(root)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("SALI_WORKS_ROOT", None)
        else:
            os.environ["SALI_WORKS_ROOT"] = previous

TEST_DB = "sali_test"


@pytest.fixture(scope="session")
def test_settings() -> Settings:
    return Settings(db=DbSettings(name=TEST_DB), model=ModelSettings(provider="fake"))


async def _reachable(settings: Settings) -> bool:
    try:
        conn = await asyncpg.connect(
            host=settings.db.host, user=settings.db.user, database=settings.db.name
        )
        await conn.close()
        return True
    except Exception:  # noqa: BLE001 - unreachable DB just means "skip db tests"
        return False


@pytest.fixture(scope="session")
def db_available(test_settings: Settings) -> bool:
    return asyncio.run(_reachable(test_settings))


@pytest.fixture
async def db_conn(test_settings: Settings, db_available: bool) -> AsyncIterator[Any]:
    if not db_available:
        pytest.skip("Postgres not reachable (run scripts/bootstrap_db.sh)")
    from sali.db.migrations.runner import apply_migrations
    from sali.db.pool import init_connection

    conn = await asyncpg.connect(
        host=test_settings.db.host,
        user=test_settings.db.user,
        database=test_settings.db.name,
        server_settings={"search_path": "sali, public"},
    )
    await init_connection(conn)  # jsonb codec
    await apply_migrations(conn)  # idempotent; commits schema once
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()  # discard all test writes
        await conn.close()


@pytest.fixture
def fake_provider() -> Iterator[FakeModelProvider]:
    yield FakeModelProvider()


@pytest.fixture
def tctx() -> Any:
    """A pool-less ToolContext for testing tools directly."""
    from sali.tools.context import local_context

    return local_context()


_LOOP_TABLES = (
    "tool_audit, tool_execution, run_events, agent_runs, message, conversation, "
    "memory_evidence, memory, stm_observation, event, graph_edge, graph_node, contradiction, "
    "task_execution, task_artifact, task_step, task, schedule, document, tool_authority, "
    "discovered_tool, learning_queue, machine_baseline, "
    # Prompt 7 adaptive learning (learning_candidate FKs task with SET NULL, so it needs explicit truncate)
    "learning_candidate, behavior_proposal, learning_contradiction, skill_proposal, consolidation_run, "
    # persistent agency (activity/side_effect FK task with SET NULL; capability/workspace_cleanup standalone)
    "activity, side_effect, capability, workspace_cleanup, "
    # capability evolution (acquisition FKs task with SET NULL; external_entity FKs task with SET NULL)
    "capability_acquisition, external_entity, "
    # digital life (digital_action FKs external_entity+task; obligation/commitment mostly standalone)
    "digital_action, obligation, commitment, "
    # autonomous life (goal self-refs; initiative FKs goal; routine/person standalone)
    "initiative, goal, routine, person, "
    # natural consent (self-ref parent; FKs task with SET NULL)
    "consent_request, "
    # capability utilization (usage FKs capability with CASCADE)
    "capability_usage, "
    # conversation threads + pending questions (pending_question FKs thread with SET NULL)
    "pending_question, conversation_thread, "
    # intent revocation + resource stewardship (standalone tombstone + incident ledgers)
    "revoked_intent, resource_incident, "
    # iPhone control center: device sessions FK api_device CASCADE, enrollment_code FK api_device SET NULL
    "device_session, enrollment_code, api_device, "
    # persistent organism additions (open loops / curiosities / proactive-decision audit trail)
    "open_loop, curiosity, proactive_decision, decision_trace"
)


@pytest.fixture
async def live_pool(test_settings: Settings, db_available: bool) -> AsyncIterator[Any]:
    """A real pool to sali_test (committed writes) for tests that span multiple connections,
    e.g. the agent loop. Truncates the runtime/memory tables before and after."""
    if not db_available:
        pytest.skip("Postgres not reachable (run scripts/bootstrap_db.sh)")
    from sali.db.migrations.runner import apply_migrations
    from sali.db.pool import connect, create_pool

    conn = await connect(test_settings)
    await apply_migrations(conn)
    await conn.close()

    pool = await create_pool(test_settings)

    async def _clean() -> None:
        async with pool.acquire() as c:
            await c.execute(f"TRUNCATE {_LOOP_TABLES} RESTART IDENTITY CASCADE")
            # sali_state is a singleton (one row); truncate + re-seed so each test starts fresh.
            await c.execute("TRUNCATE sali_state")
            await c.execute("INSERT INTO sali_state (id) VALUES (true) ON CONFLICT DO NOTHING")

    await _clean()
    try:
        yield pool
    finally:
        await _clean()
        await pool.close()
