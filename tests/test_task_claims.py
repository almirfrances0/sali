"""A claim about the task is settled by the task record, not by an opinion.

Almir, watching a live run: "sali must never say confirmed or done the task while the task still
running … even though he checks, he's supposed to use the task, so the task is not wired."

Reproduced exactly on task 42b068e5: steps 1 and 2 `done`, and Sali replied "Step 3 verified — archive
is on disk … Advancing step 3 to done" while step 3 was still `pending`. He HAD checked — the archive
really was there — he simply never called advance_task, so he announced a state change he never made.
The old guard put that to an LLM judging its own reply, and a confident, accurate-sounding sentence is
precisely what such a judge waves through. Whether step 3 is done has a fact for an answer.
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.runtime.loop import _STEP_CLAIM_RE

pytestmark = pytest.mark.db


class _Pool:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def acquire(self) -> Any:
        conn = self._conn

        class _Ctx:
            async def __aenter__(self) -> Any:
                return conn

            async def __aexit__(self, *exc: Any) -> bool:
                return False

        return _Ctx()


def _loop_with(conn: Any) -> Any:
    """The checker only needs a pool; building a whole AgentLoop would drag in a provider."""
    from sali.runtime.loop import AgentLoop

    loop = object.__new__(AgentLoop)
    loop.pool = _Pool(conn)
    return loop


async def _task_with_steps(conn: Any, statuses: list[str]) -> Any:
    task_id = await conn.fetchval(
        "INSERT INTO task (objective, status) VALUES ('verify the backup script','running') "
        "RETURNING id")
    for seq, status in enumerate(statuses, start=1):
        await conn.execute(
            "INSERT INTO task_step (task_id, seq, description, status) VALUES ($1,$2,$3,$4)",
            task_id, seq, f"step {seq}", status)
    return task_id


@pytest.mark.parametrize("said", [
    "Step 3 verified — archive is on disk at ~/Pictures/archive. Advancing step 3 to done.",
    "step 3 is done",
    "marking step 3 complete",
    "step 3 -> done",
])
def test_the_shapes_he_actually_writes_are_recognised(said: str) -> None:
    assert _STEP_CLAIM_RE.search(said)


@pytest.mark.parametrize("said", [
    "I'll start on step 3 next.",
    "Step 3 is going to need the archive path first.",
    "what does step 3 involve?",
])
def test_talking_about_a_step_is_not_claiming_it(said: str) -> None:
    assert not _STEP_CLAIM_RE.search(said)


async def test_the_live_failure_is_caught(db_conn: Any) -> None:
    """The exact reply, against the exact step state it was written under."""
    task_id = await _task_with_steps(db_conn, ["done", "done", "pending", "pending"])
    conflict = await _loop_with(db_conn)._task_claim_conflict(
        task_id, "Step 3 verified — archive is on disk. Advancing step 3 to done.")
    assert conflict, "a claim contradicted by the record must be caught"
    assert "step 3" in conflict.lower()
    assert "advance_task" in conflict, "he must be told how to actually land it"


async def test_a_true_claim_passes_untouched(db_conn: Any) -> None:
    """Correctness cuts both ways: when the row agrees, nothing interrupts him."""
    task_id = await _task_with_steps(db_conn, ["done", "done", "done", "done"])
    assert await _loop_with(db_conn)._task_claim_conflict(task_id, "Step 3 is done. All finished.") == ""


async def test_claiming_the_whole_task_while_steps_remain_is_caught(db_conn: Any) -> None:
    """The general form of the same lie — "done" with no step named."""
    task_id = await _task_with_steps(db_conn, ["done", "pending"])
    conflict = await _loop_with(db_conn)._task_claim_conflict(task_id, "All done — that's finished.")
    assert conflict and "step 2 is pending" in conflict


async def test_the_correction_states_the_real_state_rather_than_scolding(db_conn: Any) -> None:
    task_id = await _task_with_steps(db_conn, ["done", "pending", "pending"])
    conflict = await _loop_with(db_conn)._task_claim_conflict(task_id, "step 2 is complete")
    assert "step 1 is done" in conflict and "step 2 is pending" in conflict
    assert "lie" not in conflict.lower() and "wrong" not in conflict.lower()
