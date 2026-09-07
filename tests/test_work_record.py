"""Answer questions about finished work from the record, not from imagination.

THE DEFECT (2026-09-07 00:30). Almir: "why the task says failed?" Sali: "...something went wrong when
step five tried to create or access that directory — possibly a permissions issue or a race
condition..." Every causal clause invented, while the true answer sat in the task's own archived
record. He didn't lack the data; he didn't look.

The negatives matter as much as the positives. A confident paragraph about the WRONG task is worse
than no block at all, so almost every test here asserts silence.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sali.verify import work_record as W
from sali.verify.work_record import DEICTIC, NAMED, WorkRecord, classify_question, render

EDT = timezone(timedelta(hours=-4))
NOW = datetime(2026, 9, 7, 0, 30, 19, tzinfo=EDT)


# ── the text gate ────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "why the task says failed?",              # THE real message, broken grammar and all
    "why did the task fail?",
    "what went wrong with the task?",
    "why was the task abandoned?",
    "did you finish it?",
    "did you actually finish it?",
    "sali what happened with the task",       # vocative, no question mark
    "what's the status of the task?",         # contraction
    "task failed why",                        # trailing interrogative, no punctuation
    "the task failed. why?",                  # split across two sentences
    "can you tell me why the task failed?",   # politeness stripped, not vetoed
    "what happened to the nexus website?",
    "why did the nexus task fail",
])
def test_a_question_about_finished_work_is_recognised(text: str) -> None:
    assert classify_question(text) is not None, text


@pytest.mark.parametrize("text", [
    # real messages from the transcript that must stay silent
    "sali why ssh not working?",
    "what is the pc status sali",
    "i did not call that command sali, what are you talking about",
    "the site was good sali",
    "hi sali",
    "thanks sali",
    "ok do it",
    # a different subject entirely — deny beats allow
    "why did that command fail?",
    "did you send it?",
    "why did my download fail?",
    "what went wrong with the ssh connection?",
    "why do my builds keep failing on github?",
    "the build failed on my laptop just now",
    "what happened to the file i sent you?",
    "is docker done installing?",
    # the future has no record
    "will the task fail?",
    "is the task going to fail?",
    # an order, not a question
    "finish the task please",
    "retry the task",
    "can you make the task finish faster?",
    # an assertion, not a question
    "the task failed",
    "ok the task failed",
    "the task failed. i fixed it myself.",
    "design a website with 4 pages",
])
def test_it_stays_silent_on_everything_else(text: str) -> None:
    assert classify_question(text) is None, text


def test_the_referent_kind_is_right() -> None:
    assert classify_question("why the task says failed?")[0] == DEICTIC
    assert classify_question("did you finish it?")[0] == DEICTIC
    kind, words = classify_question("what happened with the nexus site?")
    assert kind == NAMED and "nexus" in words


def test_a_long_paste_is_not_scanned() -> None:
    assert classify_question("why did the task fail? " + "x" * 900) is None


# ── cost: nothing is touched on a turn that asks nothing ─────────────────────────────────────────

class _ExplodingPool:
    """Any DB access at all is a failure of the laziness contract."""
    def acquire(self):  # noqa: ANN201
        raise AssertionError("the probe must not touch the pool on a non-matching turn")


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["hi sali", "thanks", "why did that command fail?", ""])
async def test_no_database_work_when_the_gate_does_not_fire(text: str) -> None:
    assert await W.check_work_question(_ExplodingPool(), text, now=NOW) is None


@pytest.mark.asyncio
async def test_it_fails_open_when_the_database_is_unhappy() -> None:
    """A broken probe must cost the turn nothing — same contract as check_claims."""
    class _Broken:
        def acquire(self):  # noqa: ANN201
            raise RuntimeError("pool is gone")
    assert await W.check_work_question(_Broken(), "why did the task fail?", now=NOW) is None


# ── resolution, against a fake record ────────────────────────────────────────────────────────────

class _Conn:
    def __init__(self, tasks, events=(), steps=()):
        self._tasks, self._events, self._steps = tasks, events, steps

    async def fetch(self, sql, *args):  # noqa: ANN001
        if "FROM task " in sql and "task_step" not in sql:
            lo, hi = args[0] - args[1], args[0]
            return [t for t in self._tasks if lo < t["ended_at"] <= hi and t["status"] in args[2]]
        if "task.finished" in sql:
            lo, hi = args[0] - args[1], args[0]
            return [e for e in self._events if lo < e["ended_at"] <= hi]
        if "task_step" in sql:
            return list(self._steps)
        return []


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):  # noqa: ANN201
        conn = self._conn
        class _Ctx:
            async def __aenter__(self):  # noqa: ANN204
                return conn
            async def __aexit__(self, *a):  # noqa: ANN204
                return False
        return _Ctx()


def _task(tid, objective, status, ended, result=None, review=None):
    return {"id": tid, "objective": objective, "status": status, "result": result,
            "last_review_summary": review, "ended_at": ended, "source": "task_row"}


@pytest.mark.asyncio
async def test_it_answers_from_the_record(monkeypatch) -> None:
    monkeypatch.setattr(W, "_read_archive", lambda _id: (None, None))
    pool = _Pool(_Conn([_task("t1", "Build the Nexus website", "failed",
                              NOW - timedelta(hours=5),
                              result="Task stalled — completion announced while step 5 was running")]))
    rec = await W.check_work_question(pool, "why the task says failed?", now=NOW)
    assert rec is not None and rec.task_id == "t1"
    block = render(rec).lower()
    assert "already ended" in block and "history, not current state" in block
    assert "failed" in block and "hours ago" in block
    assert "task stalled" in block


@pytest.mark.asyncio
async def test_a_task_that_ended_after_the_question_is_never_the_answer(monkeypatch) -> None:
    """CAUSALITY. The successful successor was created two minutes AFTER Almir asked; narrating it
    would answer a question about the failure with a report of a triumph."""
    monkeypatch.setattr(W, "_read_archive", lambda _id: (None, None))
    pool = _Pool(_Conn([
        _task("older-failed", "Build the site", "failed", NOW - timedelta(hours=5), result="stalled"),
        _task("newer-done", "Build the site again", "done", NOW + timedelta(hours=1)),
    ]))
    rec = await W.check_work_question(pool, "why the task says failed?", now=NOW)
    assert rec is not None and rec.task_id == "older-failed"


@pytest.mark.asyncio
async def test_a_failure_question_prefers_the_failure(monkeypatch) -> None:
    monkeypatch.setattr(W, "_read_archive", lambda _id: (None, None))
    pool = _Pool(_Conn([
        _task("done-newer", "Something else", "done", NOW - timedelta(minutes=10)),
        _task("failed-older", "Build the site", "failed", NOW - timedelta(hours=3), result="stalled"),
    ]))
    rec = await W.check_work_question(pool, "why did the task fail?", now=NOW)
    assert rec.task_id == "failed-older"


@pytest.mark.asyncio
async def test_an_ambiguous_name_resolves_to_silence(monkeypatch) -> None:
    """Two candidates match "website". Recency must NEVER break the tie — the name was the
    disambiguator, and it failed to disambiguate."""
    monkeypatch.setattr(W, "_read_archive", lambda _id: (None, None))
    pool = _Pool(_Conn([
        _task("a", "Build the marketing website", "failed", NOW - timedelta(days=1)),
        _task("b", "Rebuild the website footer", "failed", NOW - timedelta(days=2)),
    ]))
    assert await W.check_work_question(pool, "what happened with the website?", now=NOW) is None


@pytest.mark.asyncio
async def test_a_deictic_question_will_not_reach_past_a_day(monkeypatch) -> None:
    monkeypatch.setattr(W, "_read_archive", lambda _id: (None, None))
    pool = _Pool(_Conn([_task("old", "Build the site", "failed", NOW - timedelta(hours=25))]))
    assert await W.check_work_question(pool, "why did the task fail?", now=NOW) is None


@pytest.mark.asyncio
async def test_abandoned_work_gets_two_lines_and_no_invented_reason(monkeypatch) -> None:
    monkeypatch.setattr(W, "_read_archive", lambda _id: (None, None))
    pool = _Pool(_Conn([_task("x", "Some abandoned thing", "abandoned", NOW - timedelta(hours=2))]))
    rec = await W.check_work_question(pool, "what happened with the task?", now=NOW)
    block = render(rec)
    assert "ABANDONED" in block
    assert "recorded when it stopped" not in block and "steps finished" not in block


# ── the render says only what it can support ─────────────────────────────────────────────────────

def _rec(**kw):
    base = dict(task_id="t", objective="Build the site", status="failed", ended_at=NOW,
                age_s=3600.0, kind=DEICTIC, source="task_row")
    base.update(kw)
    return WorkRecord(**base)


def test_the_block_never_instructs() -> None:
    """Brain-audit turns 4-5 stripped the prohibitions out of the prompt; this must not smuggle them
    back in. It states facts and stops."""
    block = render(_rec(reason="it stalled", reason_source="record", done=4, total=6))
    for banned in ("you should", "you must", "make sure", "do not", "remember to", "be sure"):
        assert banned not in block.lower()


def test_the_age_appears_relative_and_absolute() -> None:
    """The one way this becomes a fresh hallucination is an old failure read as current state."""
    block = render(_rec(age_s=5 * 3600)).lower()
    assert "ago" in block and "already ended" in block
    assert "history, not current state" in block


def test_chat_prose_is_labelled_as_chat_not_as_a_record() -> None:
    """Mislabelling provenance is the exact failure this module exists to prevent."""
    assert "told Almir at the time" in render(_rec(reason="I think it broke", reason_source="chat"))
    assert "recorded when it stopped" in render(_rec(reason="stalled", reason_source="record"))


def test_a_vanished_record_says_so_plainly() -> None:
    block = render(_rec(thin=True))
    assert "detailed record is gone" in block
