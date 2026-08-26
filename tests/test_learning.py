"""Self-learning (§17-19): command normalization, evidence-gated procedures, failure records."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from sali.learning.episodes import consolidate_stm, prune_stm
from sali.learning.failures import record_failures
from sali.learning.mining import normalize_command, sequences_by_run, signature
from sali.learning.procedures import learn_procedures
from sali.provider.base import ChatResult
from sali.provider.fake import FakeModelProvider


# ---- deterministic mining (no model, no DB) ---------------------------------------------------
def test_normalize_command_reduces_to_action_skeleton() -> None:
    assert normalize_command("docker compose up -d /srv/app") == "docker compose up"
    assert normalize_command("docker compose build") == "docker compose build"
    assert normalize_command("git pull") == "git pull"
    assert normalize_command("ls") == "ls"
    assert normalize_command("./deploy.sh --prod") == ""  # a path/script → no stable skeleton


def test_sequences_group_by_run_and_count_evidence() -> None:
    r1, r2, r3 = uuid4(), uuid4(), uuid4()
    rows = [
        {"run_id": r1, "command": "git pull"},
        {"run_id": r1, "command": "docker compose up -d"},
        {"run_id": r2, "command": "git pull"},
        {"run_id": r2, "command": "docker compose up -d"},
        {"run_id": r3, "command": "htop"},  # single command → not a sequence
    ]
    seqs = sequences_by_run(rows)
    key = ("git pull", "docker compose up")
    assert seqs[key] == {r1, r2}  # the deploy sequence appeared in two distinct runs
    assert all(len(k) >= 2 for k in seqs)  # single-command runs never form a procedure
    assert signature(key) == signature(key) and len(signature(key)) == 16


# ---- procedures + failures (DB) ---------------------------------------------------------------
pytestmark = pytest.mark.db


async def _exec(conn: Any, run_id: Any, command: str, when: datetime, *,
                status: str = "verified_success", success: bool = True) -> None:
    await conn.execute(
        "INSERT INTO tool_execution (run_id, tool_name, status, danger_level, plan, success, started_at) "
        "VALUES ($1,'execute_command',$2::tool_status,0,$3,$4,$5)",
        run_id, status, {"args": {"command": command}}, success, when,
    )


async def test_learns_procedure_only_with_repeated_evidence(db_conn: Any) -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for i, run in enumerate((uuid4(), uuid4())):  # same sequence in TWO runs
        await _exec(db_conn, run, "git pull", base + timedelta(seconds=i * 10))
        await _exec(db_conn, run, "docker compose up -d", base + timedelta(seconds=i * 10 + 1))
    solo = uuid4()  # a different sequence seen only ONCE → must not be learned
    await _exec(db_conn, solo, "make", base + timedelta(seconds=100))
    await _exec(db_conn, solo, "make install", base + timedelta(seconds=101))

    fake = FakeModelProvider(responses=[ChatResult("Docker deploy", None, [], 3, 3, "fake")])
    learned = await learn_procedures(db_conn, fake, threshold=2)

    assert len(learned) == 1  # only the repeated one crossed the evidence bar (§17)
    assert learned[0].name == "Docker deploy" and learned[0].evidence == 2
    assert learned[0].steps == ["git pull", "docker compose up"]
    row = await db_conn.fetchrow(
        "SELECT content, structured, source FROM memory "
        "WHERE layer='procedural' AND valid_until IS NULL"
    )
    assert "Docker deploy" in row["content"] and row["structured"]["evidence"] == 2
    assert row["source"] == "inference"  # learned, so honestly weaker than a first-hand observation


async def test_records_only_fixed_failures_and_does_not_duplicate(db_conn: Any) -> None:
    run = uuid4()
    t0 = datetime.now(UTC) - timedelta(minutes=1)  # recent, so it's inside the recording window
    await db_conn.execute(
        "INSERT INTO agent_runs (run_id, session_id, user_input, state, status) "
        "VALUES ($1,$2,'deploy the app','done','completed')", run, uuid4(),
    )
    # A bare one-off failure with no later fix is NOT a lesson (§18 = failure→correction) — skipped.
    await db_conn.execute(
        "INSERT INTO tool_execution (run_id, tool_name, status, danger_level, plan, success, error, started_at) "
        "VALUES ($1,'read_file','verified_failure'::tool_status,0,$2,false,'no such file',$3)",
        run, {"args": {"path": "/nope"}}, t0,
    )
    assert await record_failures(db_conn) == 0  # bare failure → not recorded

    # A failure that a LATER step in the same run fixed IS recorded — the lesson is the correction.
    await db_conn.execute(
        "INSERT INTO tool_execution (run_id, tool_name, status, danger_level, plan, success, error, started_at) "
        "VALUES ($1,'execute_command','verified_failure'::tool_status,0,$2,false,$3,$4)",
        run, {"args": {"command": "docker compose up -d"}}, "container failed health check", t0,
    )
    await db_conn.execute(
        "INSERT INTO tool_execution (run_id, tool_name, status, danger_level, plan, success, started_at) "
        "VALUES ($1,'execute_command','verified_success'::tool_status,0,$2,true,$3)",
        run, {"args": {"command": "docker compose --env-file .env up -d"}}, t0 + timedelta(seconds=5),
    )
    assert await record_failures(db_conn) == 1
    row = await db_conn.fetchrow(
        "SELECT content FROM memory WHERE layer='episodic' AND claim_key LIKE 'failure:%' "
        "AND valid_until IS NULL"
    )
    assert "docker compose up" in row["content"] and "health check" in row["content"]
    assert "What fixed it" in row["content"] and "--env-file" in row["content"]  # the correction
    assert await record_failures(db_conn) == 0  # idempotent — the same lesson isn't re-recorded


async def test_relearning_a_procedure_reuses_the_name_and_does_not_churn(db_conn: Any) -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for i, run in enumerate((uuid4(), uuid4())):
        await _exec(db_conn, run, "git pull", base + timedelta(seconds=i * 10))
        await _exec(db_conn, run, "docker compose up -d", base + timedelta(seconds=i * 10 + 1))
    # Only ONE naming response scripted — the second pass must reuse the stored name (no model call).
    fake = FakeModelProvider(responses=[ChatResult("Docker deploy", None, [], 3, 3, "fake")])
    await learn_procedures(db_conn, fake, threshold=2)
    await learn_procedures(db_conn, fake, threshold=2)  # re-consolidate
    current = await db_conn.fetch(
        "SELECT content FROM memory WHERE layer='procedural' AND valid_until IS NULL"
    )
    assert len(current) == 1  # exactly one current — no supersession churn
    assert "Docker deploy" in current[0]["content"]  # reused the name, not a re-generated one


async def test_failure_records_the_fix_when_a_later_step_succeeded(db_conn: Any) -> None:
    run = uuid4()
    base = datetime.now(UTC) - timedelta(minutes=1)  # recent, inside the recording window
    await db_conn.execute(
        "INSERT INTO tool_execution (run_id,tool_name,status,danger_level,plan,success,error,started_at) "
        "VALUES ($1,'execute_command','verified_failure'::tool_status,0,$2,false,'missing env var',$3)",
        run, {"args": {"command": "docker compose up -d"}}, base,
    )
    await db_conn.execute(
        "INSERT INTO tool_execution (run_id,tool_name,status,danger_level,plan,success,started_at) "
        "VALUES ($1,'execute_command','verified_success'::tool_status,0,$2,true,$3)",
        run, {"args": {"command": "docker compose --env-file .env up -d"}}, base + timedelta(seconds=5),
    )
    assert await record_failures(db_conn) == 1
    row = await db_conn.fetchrow(
        "SELECT content FROM memory WHERE layer='episodic' AND claim_key LIKE 'failure:%' "
        "AND valid_until IS NULL"
    )
    assert "What fixed it" in row["content"] and "--env-file" in row["content"]  # §18 correction


async def test_consolidate_stm_folds_into_one_episode_and_clears_raw(db_conn: Any) -> None:
    for i in range(6):
        await db_conn.execute(
            "INSERT INTO stm_observation (kind, content, source) "
            "VALUES ('turn',$1,'conversation'::memory_source)", f"Almir asked about topic {i}",
        )
    fake = FakeModelProvider(responses=[ChatResult("Almir explored a few topics.", None, [], 3, 3, "fake")])
    assert await consolidate_stm(db_conn, fake, min_obs=5) == 1
    ep = await db_conn.fetchrow(
        "SELECT content FROM memory WHERE layer='episodic' AND valid_until IS NULL "
        "ORDER BY created_at DESC LIMIT 1"
    )
    assert "explored a few topics" in ep["content"]
    live = await db_conn.fetchval("SELECT count(*) FROM stm_observation WHERE expires_at > now()")
    assert live == 0  # the raw observations were folded away


async def test_consolidate_stm_skips_below_threshold_and_prune_drops_expired(db_conn: Any) -> None:
    await db_conn.execute(
        "INSERT INTO stm_observation (kind, content, source) "
        "VALUES ('turn','just one','conversation'::memory_source)"
    )
    assert await consolidate_stm(db_conn, FakeModelProvider(), min_obs=5) == 0  # too few → no episode
    await db_conn.execute(
        "INSERT INTO stm_observation (kind, content, source, expires_at) "
        "VALUES ('turn','stale','conversation'::memory_source, now() - interval '1 hour')"
    )
    assert await prune_stm(db_conn) == 1  # only the expired one is dropped


async def test_consolidate_skips_when_a_pass_already_holds_the_lock(live_pool: Any) -> None:
    # Two consolidation passes (the agent loop + the observe daemon) must never race into a
    # duplicate-key crash — the advisory lock serialises them; the second skips cleanly.
    from sali.learning.service import _CONSOLIDATE_LOCK, LearningService

    async with live_pool.acquire() as holder, holder.transaction():
        await holder.fetchval("SELECT pg_advisory_xact_lock($1)", _CONSOLIDATE_LOCK)  # hold it
        result = await LearningService(live_pool, FakeModelProvider()).consolidate()
        assert not result.did_something  # blocked pass skipped, no crash
