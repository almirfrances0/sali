"""The full journaled agent loop, end to end (with a scripted fake model)."""

from __future__ import annotations

from typing import Any

import pytest

from sali.config.settings import DbSettings, ModelSettings, Settings
from sali.context.engine import ContextEngine
from sali.core.ids import new_id
from sali.provider.base import ChatResult, ToolCall
from sali.provider.fake import FakeModelProvider
from sali.retrieval.service import RetrievalService
from sali.runtime.loop import _EMPTY_FALLBACK, _MAX_TOOL_FAILURES, AgentLoop
from sali.security.confirm import AutoAllowConfirmer
from sali.security.policy import PolicyEngine
from sali.tools.registry import default_registry

pytestmark = pytest.mark.db


def _settings() -> Settings:
    return Settings(db=DbSettings(name="sali_test"), model=ModelSettings(provider="fake"))


def _loop(pool: Any, provider: FakeModelProvider) -> AgentLoop:
    return AgentLoop(
        pool=pool,
        provider=provider,
        retrieval=RetrievalService(pool, provider),
        context=ContextEngine(provider, ctx_tokens=8192),
        registry=default_registry(),
        policy=PolicyEngine(),
        confirmer=AutoAllowConfirmer(),
        settings=_settings(),
    )


async def test_empty_final_answer_is_wrapped_up_in_voice(live_pool: Any) -> None:
    # The model gives up with an empty reply; the guard has it explain itself instead of showing blank.
    fake = FakeModelProvider(responses=[
        ChatResult("", None, [], 5, 1, "fake"),  # empty final — the "stopped without anything" case
        ChatResult("I couldn't reach the server — the password auth failed.", None, [], 5, 1, "fake"),
    ])
    result = await _loop(live_pool, fake).run("check the vps")
    assert "password auth failed" in result.text  # a real explanation, not silence


async def test_empty_final_never_records_a_blank_reply(live_pool: Any) -> None:
    # Even if the wrap-up ALSO comes back empty, Sali says something — never a zero-char response.
    fake = FakeModelProvider(responses=[
        ChatResult("", None, [], 5, 1, "fake"),
        ChatResult("", None, [], 5, 1, "fake"),
    ])
    result = await _loop(live_pool, fake).run("do the impossible")
    assert result.text == _EMPTY_FALLBACK and result.text.strip()


async def test_loop_calls_tool_then_answers_and_journals(live_pool: Any) -> None:
    fake = FakeModelProvider(
        responses=[
            ChatResult("", None, [ToolCall("memory_info", {})], 10, 5, "fake"),
            ChatResult("You have plenty of memory available.", None, [], 20, 8, "fake"),
        ]
    )
    result = await _loop(live_pool, fake).run("how much memory is free right now?")

    assert result.text == "You have plenty of memory available."
    assert result.tool_calls == 1

    async with live_pool.acquire() as c:
        run = await c.fetchrow(
            "SELECT status, state FROM agent_runs WHERE run_id=$1", result.run_id
        )
        assert run["status"] == "completed"
        assert run["state"] == "done"

        te = await c.fetchrow(
            "SELECT tool_name, status, success FROM tool_execution WHERE run_id=$1", result.run_id
        )
        assert te["tool_name"] == "memory_info"
        assert te["status"] == "verified_success"  # never assumed — actually verified
        assert te["success"] is True

        # journaled: at least retrieve + 2 llm_calls + tool + respond
        events = await c.fetchval(
            "SELECT count(*) FROM run_events WHERE run_id=$1", result.run_id
        )
        assert events >= 5
        llm_calls = await c.fetchval(
            "SELECT count(*) FROM run_events WHERE run_id=$1 AND kind='llm_call'", result.run_id
        )
        assert llm_calls == 2
        audits = await c.fetchval(
            "SELECT count(*) FROM tool_audit WHERE run_id=$1", result.run_id
        )
        assert audits == 1


async def test_loop_denies_unknown_tool_but_continues(live_pool: Any) -> None:
    fake = FakeModelProvider(
        responses=[
            ChatResult("", None, [ToolCall("rm_rf_slash", {})], 5, 2, "fake"),
            ChatResult("I could not use that tool, but I'm still here.", None, [], 5, 5, "fake"),
        ]
    )
    result = await _loop(live_pool, fake).run("do something dangerous")
    assert "still here" in result.text
    async with live_pool.acquire() as c:
        run = await c.fetchrow("SELECT status FROM agent_runs WHERE run_id=$1", result.run_id)
        assert run["status"] == "completed"  # an unknown tool never crashes the loop
        # no tool_execution row for a tool that never existed
        assert await c.fetchval(
            "SELECT count(*) FROM tool_execution WHERE run_id=$1", result.run_id
        ) == 0


async def test_loop_recover_surfaces_interrupted_run(live_pool: Any) -> None:
    # Simulate a crash: a run left 'running' in EXECUTE_TOOL with a non-idempotent tool.
    async with live_pool.acquire() as c:
        run_id = await c.fetchval(
            "INSERT INTO agent_runs (run_id, session_id, user_input, state, status) "
            "VALUES (gen_random_uuid(), gen_random_uuid(), 'x', 'execute_tool', 'running') "
            "RETURNING run_id"
        )
        await c.execute(
            "INSERT INTO tool_execution (run_id, tool_name, status, danger_level, approved_by, plan) "
            "VALUES ($1, 'git_pull', 'executing', 2, 'policy:confirm', '{}')",
            run_id,
        )

    fake = FakeModelProvider()
    resolved = await _loop(live_pool, fake).recover()

    assert len(resolved) == 1
    assert resolved[0]["was_state"] == "execute_tool"
    # git_pull is a known, non-idempotent tool → verify against reality, never blind re-run (M15).
    assert resolved[0]["action"] == "verify_then_continue"
    async with live_pool.acquire() as c:
        status = await c.fetchval("SELECT status FROM agent_runs WHERE run_id=$1", run_id)
        assert status == "aborted"


async def test_astream_streams_tokens_then_final(live_pool: Any) -> None:
    fake = FakeModelProvider(responses=[ChatResult("hello there friend", None, [], 3, 3, "fake")])
    kinds: list[str] = []
    final_text = ""
    async for event in _loop(live_pool, fake).astream("hi", session_id=new_id()):
        kinds.append(event.kind)
        if event.kind == "final":
            final_text = event.text
    assert "token" in kinds and "final" in kinds  # the answer streamed, then finalized
    assert final_text == "hello there friend"


async def test_astream_emits_tool_events(live_pool: Any) -> None:
    fake = FakeModelProvider(
        responses=[
            ChatResult("", None, [ToolCall("memory_info", {})], 3, 2, "fake"),
            ChatResult("plenty free", None, [], 3, 3, "fake"),
        ]
    )
    starts = [
        e async for e in _loop(live_pool, fake).astream("ram?", session_id=new_id())
        if e.kind == "tool" and e.data.get("phase") == "start"
    ]
    assert len(starts) == 1 and starts[0].data["name"] == "memory_info"


async def test_zero_tool_reply_is_taken_at_face_value(live_pool: Any) -> None:
    # A purely conversational reply — a greeting, or a first-step announcement with no work started
    # yet — is taken at face value: NO self-judge inference, NO follow-through nudge. This is the
    # deliberate trade that keeps greetings/recall instant (the stall-judge is a full inference).
    # Partial-work stalls (tool_calls>0) ARE still caught — see the next test.
    fake = FakeModelProvider(
        responses=[
            ChatResult("Let's start by checking your memory.", None, [], 5, 3, "fake"),
        ]
    )
    result = await _loop(live_pool, fake).run("check my memory")
    assert result.tool_calls == 0
    assert result.text == "Let's start by checking your memory."
    async with live_pool.acquire() as c:
        judged = await c.fetchval(
            "SELECT count(*) FROM run_events WHERE run_id=$1 AND kind='stall_judge'", result.run_id)
        followed = await c.fetchval(
            "SELECT count(*) FROM run_events WHERE run_id=$1 AND kind='follow_through'", result.run_id)
        assert judged == 0 and followed == 0  # a zero-tool turn never pays for the judge


async def test_loop_follows_through_after_partial_work(live_pool: Any) -> None:
    # Sali does PART of a multi-step task (one tool), then says it'll "keep building" and stops —
    # the mid-task stall the old tool_calls==0 gate missed. It must be caught and pushed to finish.
    fake = FakeModelProvider(
        responses=[
            ChatResult("", None, [ToolCall("memory_info", {})], 4, 2, "fake"),           # step 1
            ChatResult("Made a start. I'll keep building the rest.", None, [], 4, 3, "fake"),  # stalls
            ChatResult("STALLED", None, [], 1, 1, "fake"),                               # self-judged
            ChatResult("", None, [ToolCall("disk_info", {})], 4, 2, "fake"),             # follows through
            ChatResult("All done — everything's built.", None, [], 4, 3, "fake"),        # finishes
            ChatResult("DONE", None, [], 1, 1, "fake"),                                  # self-judged
        ]
    )
    result = await _loop(live_pool, fake).run("build the thing, it has several steps")
    assert result.tool_calls == 2  # both steps ran — incl. the one after the mid-task nudge
    assert "All done" in result.text
    async with live_pool.acquire() as c:
        followed = await c.fetchval(
            "SELECT count(*) FROM run_events WHERE run_id=$1 AND kind='follow_through'",
            result.run_id,
        )
        assert followed == 1


async def test_loop_breaks_a_repeated_tool_loop(live_pool: Any) -> None:
    # Sali re-runs the same tool over and over (the investigate-forever loop). The loop guard must
    # inject a course-correction once it's repeated enough, so it doesn't spin to the iteration cap.
    same = ToolCall("memory_info", {})
    fake = FakeModelProvider(
        responses=[
            ChatResult("", None, [same], 3, 2, "fake"),   # 1st  (repeats 0)
            ChatResult("", None, [same], 3, 2, "fake"),   # 2nd  (repeats 1)
            ChatResult("", None, [same], 3, 2, "fake"),   # 3rd  (repeats 2)
            ChatResult("", None, [same], 3, 2, "fake"),   # 4th  (repeats 3 → loop_break)
            ChatResult("Okay, stopping — here's what I found.", None, [], 3, 3, "fake"),  # finishes
            ChatResult("DONE", None, [], 1, 1, "fake"),   # self-judgement
        ]
    )
    result = await _loop(live_pool, fake).run("keep looking into it")
    assert "here's what I found" in result.text
    async with live_pool.acquire() as c:
        breaks = await c.fetchval(
            "SELECT count(*) FROM run_events WHERE run_id=$1 AND kind='loop_break'", result.run_id
        )
        assert breaks >= 1  # the loop was detected and broken


async def test_repeated_failing_tool_is_circuit_broken(live_pool: Any) -> None:
    # The observed memory-hunting bug: the model retried the SAME failing call (list_directory at a
    # path that doesn't exist) over and over. After _MAX_TOOL_FAILURES identical failures the loop
    # refuses to dispatch it again and hands the failure back as a factual result, so it can't loop.
    bad = ToolCall("list_directory", {"path": "/home/sali/memory"})  # nonexistent → always fails
    fake = FakeModelProvider(
        responses=[
            ChatResult("", None, [bad], 3, 2, "fake"),   # 1st dispatch → fails
            ChatResult("", None, [bad], 3, 2, "fake"),   # 2nd dispatch → fails
            ChatResult("", None, [bad], 3, 2, "fake"),   # 3rd → circuit-broken, NOT dispatched
            ChatResult("That folder isn't there — I'll answer from what I already know.", None, [], 3, 3, "fake"),
            ChatResult("DONE", None, [], 1, 1, "fake"),   # self-judge on the tool_calls>0 final reply
        ]
    )
    result = await _loop(live_pool, fake).run("look in my memory folder")
    assert "isn't there" in result.text
    async with live_pool.acquire() as c:
        dispatched = await c.fetchval(
            "SELECT count(*) FROM tool_execution WHERE run_id=$1 AND tool_name='list_directory'",
            result.run_id)
        breaks = await c.fetchval(
            "SELECT count(*) FROM run_events WHERE run_id=$1 AND kind='tool_circuit_break'",
            result.run_id)
        assert dispatched == _MAX_TOOL_FAILURES  # executed exactly twice, then refused
        assert breaks >= 1


async def test_interrupted_turn_is_marked_aborted_not_left_running(live_pool: Any) -> None:
    # A cancelled turn (Ctrl-C / WebSocket disconnect) must NOT leave the run stuck as 'running' —
    # it's marked aborted and the cancellation still propagates, so state never leaks.
    import asyncio

    class _CancelStream(FakeModelProvider):
        async def chat_stream(self, *args: Any, **kwargs: Any) -> Any:
            raise asyncio.CancelledError
            yield  # pragma: no cover - makes this an async generator

    sid = new_id()
    loop = _loop(live_pool, _CancelStream())
    with pytest.raises(asyncio.CancelledError):
        async for _ in loop.astream("do something", session_id=sid):
            pass

    async with live_pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT status, state FROM agent_runs WHERE session_id=$1 ORDER BY started_at DESC LIMIT 1",
            sid)
    assert row["status"] == "aborted" and row["state"] == "aborted"


async def test_repeated_reply_stops_the_follow_through(live_pool: Any) -> None:
    # Sali did a tool, then gives the SAME stalled reply twice. The repetition guard must take the
    # repeat at face value instead of nudging it to say the same thing again (the "over and over" bug).
    fake = FakeModelProvider(responses=[
        ChatResult("", None, [ToolCall("memory_info", {})], 3, 2, "fake"),  # a tool ran → tool_calls>0
        ChatResult("Let me resend that now.", None, [], 3, 3, "fake"),       # stalled reply
        ChatResult("STALLED", None, [], 1, 1, "fake"),                       # judged → one nudge
        ChatResult("Let me resend that now.", None, [], 3, 3, "fake"),       # identical repeat → stop
    ])
    result = await _loop(live_pool, fake).run("send it", session_id=new_id())
    assert "resend" in result.text
    async with live_pool.acquire() as c:
        followed = await c.fetchval(
            "SELECT count(*) FROM run_events WHERE run_id=$1 AND kind='follow_through'", result.run_id)
    assert followed == 1  # nudged once, then the repeat was caught — not pushed to repeat again


async def test_loop_does_not_nudge_a_complete_answer(live_pool: Any) -> None:
    # A short answer that Sali judges DONE must NOT be nudged — it just finalizes.
    fake = FakeModelProvider(
        responses=[
            ChatResult("Your disk is about half full.", None, [], 4, 4, "fake"),  # the answer
            ChatResult("DONE", None, [], 1, 1, "fake"),                            # self-judgement
        ]
    )
    result = await _loop(live_pool, fake).run("how full is my disk")
    assert result.text == "Your disk is about half full."
    async with live_pool.acquire() as c:
        followed = await c.fetchval(
            "SELECT count(*) FROM run_events WHERE run_id=$1 AND kind='follow_through'",
            result.run_id,
        )
        assert followed == 0


async def test_stalled_is_model_judged_with_no_length_cutoff() -> None:
    # No phrase list, no length cutoff: every non-empty tool-less reply is put to the model, so a
    # stall hiding at the end of a LONG explanation is still caught.
    stalled = _loop(None, FakeModelProvider(responses=[ChatResult("STALLED", None, [], 1, 1, "fake")]))
    long_stall = "Here's the situation. " * 120 + "Let me write it all out now."  # ~2700 chars
    assert await stalled._stalled("build it", long_stall) is True  # long → still judged & caught
    assert await stalled._stalled("hi", "   ") is False  # empty → no model call at all
    done = _loop(None, FakeModelProvider(responses=[ChatResult("DONE", None, [], 1, 1, "fake")]))
    assert await done._stalled("say hi", "Hey Almir!") is False  # judged complete


async def test_maybe_learn_schedules_consolidation_off_thread_and_throttles() -> None:
    # Memory only ACCRUES if the loop actually drives consolidation. _maybe_learn must schedule the
    # learning pass off-thread (fire-and-forget, no turn latency) and then throttle repeat calls.
    from sali.learning.model import ConsolidationResult

    ran = []

    class _FakeLearning:
        async def consolidate(self) -> ConsolidationResult:
            ran.append(1)
            return ConsolidationResult()

    loop = _loop(None, FakeModelProvider(responses=[]))
    loop.learning = _FakeLearning()

    loop._maybe_learn()
    assert loop._consolidating is not None  # scheduled, not awaited inline
    await loop._consolidating  # let the background pass finish
    assert ran == [1]

    loop._maybe_learn()  # immediately again → throttled, no second pass
    assert ran == [1]


def test_looks_like_leaked_tool_call() -> None:
    from sali.runtime.loop import _looks_like_leaked_tool_call

    assert _looks_like_leaked_tool_call('{"name": "create_file", "arguments": {"path": "/x"}}')
    assert _looks_like_leaked_tool_call('  [ {"function": {"name": "run"}} ]  ')
    assert not _looks_like_leaked_tool_call("Here's a JSON example: {\"a\": 1} in your config.")
    assert not _looks_like_leaked_tool_call("Your disk is half full.")


def test_token_gate_streams_prose_but_withholds_a_blob() -> None:
    from sali.runtime.loop import _TokenGate

    # Prose: released and then flows live, one chunk at a time, nothing lost.
    prose = _TokenGate()
    shown = "".join(prose.feed(c) for c in ["He", "llo ", "Almir", "!"])
    assert shown == "Hello Almir!"

    # Leaked tool-call blob: withheld the whole way — the raw JSON is never surfaced as tokens.
    blob = _TokenGate()
    emitted = [blob.feed(c) for c in ['{"name": ', '"create_file"', ', "arguments"', ": {}}"]]
    assert emitted == ["", "", "", ""]  # nothing shown at any point

    # Leading whitespace doesn't fool it — the first real character still decides.
    spaced = _TokenGate()
    assert spaced.feed("   \n") == ""  # undecided while only whitespace
    assert spaced.feed("Hey") == "   \nHey"  # flushes the buffered whitespace with the prose


async def test_loop_recovers_from_leaked_tool_json(live_pool: Any) -> None:
    # The model emits a tool call as raw JSON text (parser miss). The loop must NOT hand that
    # back as the answer — it recovers and finalizes on the real reply. Content isn't streamed, so
    # the JSON is never shown at all.
    fake = FakeModelProvider(
        responses=[
            ChatResult('{"name": "create_file", "arguments": {"path": "/tmp/x", "content": "hi"}}',
                       None, [], 6, 4, "fake"),
            ChatResult("Done — I saved that note for you.", None, [], 4, 3, "fake"),
        ]
    )
    events = [e async for e in _loop(live_pool, fake).astream("write a note", session_id=new_id())]
    final = next(e for e in events if e.kind == "final")
    assert final.text == "Done — I saved that note for you."  # never the raw JSON
    # the leaked JSON was never streamed as a visible token (content is revealed only when final)
    assert not any(e.kind == "token" and "create_file" in e.text for e in events)
    async with live_pool.acquire() as c:
        recovered = await c.fetchval(
            "SELECT count(*) FROM run_events WHERE kind='json_recovery'"
        )
        assert recovered >= 1


async def test_loop_retries_transient_provider_error(live_pool: Any) -> None:
    # A malformed-tool-call 500 must not crash the turn — the loop re-samples and carries on.
    from collections.abc import AsyncIterator

    from sali.core.errors import ProviderError
    from sali.provider.base import ChatChunk, ChatMessage, ToolSpec

    class FlakyProvider(FakeModelProvider):
        def __init__(self) -> None:
            super().__init__(
                responses=[ChatResult("Recovered and answered.", None, [], 3, 3, "fake")]
            )
            self._failed = False

        async def chat_stream(
            self, messages: list[ChatMessage], *, tools: list[ToolSpec] | None = None,
            options: Any = None, think: bool = False,
        ) -> AsyncIterator[ChatChunk]:
            if not self._failed:
                self._failed = True
                raise ProviderError("XML syntax error: malformed tool call")
                yield  # pragma: no cover - makes this an async generator
            async for chunk in super().chat_stream(messages, tools=tools, options=options):
                yield chunk

    result = await _loop(live_pool, FlakyProvider()).run("do the thing", session_id=new_id())
    assert "Recovered and answered." in result.text  # the retry succeeded
    async with live_pool.acquire() as c:
        retries = await c.fetchval(
            "SELECT count(*) FROM run_events WHERE run_id=$1 AND kind='provider_retry'",
            result.run_id,
        )
        assert retries == 1


def test_tool_message_bounds_huge_output() -> None:
    from sali.runtime.loop import _TOOL_OUTPUT_CAP, _tool_message

    msg = _tool_message("read_file", {"ok": True, "output": {"content": "x" * 100_000}})
    assert len(msg.content) < 100_000  # a giant result can't swallow the whole context window
    assert "truncated" in msg.content
    small = _tool_message("memory_info", {"ok": True, "output": {"free": 123}})
    assert "truncated" not in small.content and len(small.content) <= _TOOL_OUTPUT_CAP


def test_over_context_predicate() -> None:
    from sali.provider.base import ChatMessage

    loop = _loop(None, FakeModelProvider())
    loop.settings.model.ctx_default = 100  # tiny window so the estimate trips quickly
    small = [ChatMessage(role="system", content="x"), ChatMessage(role="user", content="hi")]
    assert not loop._over_context(small)  # <=3 messages: nothing to fold
    big = [ChatMessage(role="system", content="s"), ChatMessage(role="user", content="q")]
    big += [ChatMessage(role="assistant", content="detail " * 40) for _ in range(6)]
    assert loop._over_context(big)  # well past 0.65 * 100 tokens


async def test_fold_messages_keeps_system_and_task_and_notes_progress() -> None:
    from sali.provider.base import ChatMessage

    fake = FakeModelProvider(responses=[ChatResult("did A and B; C remains.", None, [], 5, 5, "fake")])
    loop = _loop(None, fake)
    messages = [
        ChatMessage(role="system", content="SYSTEM"),
        ChatMessage(role="user", content="TASK: build the thing"),
        ChatMessage(role="assistant", content="step one"),
        ChatMessage(role="tool", name="t", content="result one"),
        ChatMessage(role="assistant", content="step two"),
    ]
    folded = await loop._fold_messages(messages)
    assert len(folded) == 4  # system + original task + progress note + most-recent result verbatim
    assert folded[0].content == "SYSTEM" and folded[1].content == "TASK: build the thing"
    assert "did A and B; C remains." in folded[2].content  # the model-written carry-forward note
    assert "step two" in folded[3].content  # the most recent concrete result is kept, not lost


async def test_loop_folds_context_mid_turn_and_still_finishes(live_pool: Any) -> None:
    # A long turn: several tool round-trips push the working prompt past a (tiny) window, so the
    # loop folds and continues on its own, then finalizes — no dead-end on the context limit.
    fake = FakeModelProvider(
        responses=[
            ChatResult("", None, [ToolCall("memory_info", {})], 5, 5, "fake"),  # iter 0: a tool
            ChatResult("(folded progress note)", None, [], 5, 5, "fake"),       # the fold summary
            ChatResult("All done — here's the result.", None, [], 5, 5, "fake"),  # iter 1: answer
        ]
    )
    loop = _loop(live_pool, fake)
    loop.settings.model.ctx_default = 60  # tiny window → folds once the first tool result lands
    result = await loop.run("do a long multi-step job", session_id=new_id())
    assert "All done" in result.text
    async with live_pool.acquire() as c:
        folded = await c.fetchval(
            "SELECT count(*) FROM run_events WHERE run_id=$1 AND kind='context_folded'",
            result.run_id,
        )
        assert folded >= 1  # it compacted mid-turn and kept going


async def test_sense_short_circuits_on_tiny_input() -> None:
    # A hunch on <4 chars would be noise — and it must never touch the DB for that (pool=None).
    loop = _loop(None, FakeModelProvider())
    assert await loop.sense("hi") == ""
    assert await loop.sense("   ") == ""


async def test_sense_forms_a_hunch_from_the_graph(live_pool: Any) -> None:
    from sali.core.enums import MemorySource
    from sali.graph import writer as gw

    async with live_pool.acquire() as c:
        almir = await gw.ensure_node(
            c, node_type="person", name="Almir", canonical_key="person:almir",
            source=MemorySource.USER_EXPLICIT,
        )
        sali = await gw.ensure_node(
            c, node_type="agent", name="Sali", canonical_key="agent:sali",
            source=MemorySource.USER_EXPLICIT,
        )
        await gw.relate(
            c, src_id=almir.id, dst_id=sali.id, rel_type="uses",
            source=MemorySource.USER_EXPLICIT,
        )
    # As Almir types about Almir, the entity links in — retrieval only, no model was called.
    hunch = await _loop(live_pool, FakeModelProvider()).sense("what does Almir use")
    assert "Almir" in hunch and "Sali" in hunch


async def test_conversation_compacts_when_long(db_conn: Any) -> None:
    from sali.runtime.session import persistent_session_id  # noqa: F401 (import-shape check)

    session = new_id()
    await db_conn.execute("INSERT INTO conversation (id) VALUES ($1)", session)
    for i in range(30):
        await db_conn.execute(
            "INSERT INTO message (conversation_id, seq, role, content) VALUES ($1,$2,$3,$4)",
            session, i + 1, "user" if i % 2 == 0 else "assistant", f"turn number {i}",
        )
    loop = _loop(None, FakeModelProvider())  # pool unused by _maybe_compact / _load_history
    await loop._maybe_compact(db_conn, session)

    conv = await db_conn.fetchrow(
        "SELECT summary, summary_through_seq FROM conversation WHERE id=$1", session
    )
    assert conv["summary"] is not None
    assert conv["summary_through_seq"] == 30 - 6  # kept the last 6 verbatim
    history = await loop._load_history(db_conn, session)
    assert history[0][0] == "earlier"  # the running summary leads the history
    assert len(history) <= 9  # summary + at most 8 recent
