"""PredictionLedger — the improvement engine wired at the tool-execution seam (_handle_tool).

For an effectful tool, Sali records what it EXPECTED (learned per-tool reliability + p50 latency) and
settles it against what actually happened in decision_trace — the real signal the /cognitive-metrics
rollup was missing (observe_outcome had zero production callers, so `settled` was structurally 0). A
confident expectation violated raises an OBSERVABLE surprise event (never an autonomy actuator);
cold-start never cries wolf; the whole ledger is additive + fail-open.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

pytestmark = pytest.mark.db


class _Journal:
    def __init__(self, run_id: Any) -> None:
        self.run_id = run_id
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def event(self, kind: str, payload: dict[str, Any] | None = None, **_: Any) -> None:
        self.events.append((kind, payload or {}))


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


def _loop(pool: Any, emitted: list) -> Any:
    from sali.runtime.loop import AgentLoop

    loop = object.__new__(AgentLoop)
    loop.pool = pool

    async def _emit(conn: Any, event_type: str, subject_id: Any, payload: dict[str, Any],
                    *, subject_type: str = "agent_run") -> None:
        emitted.append((event_type, payload))

    loop._emit = _emit
    return loop


async def _tool_traces(conn: Any) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT mode, origin, expected_outcome, actual_outcome, confidence "
        "FROM decision_trace WHERE origin='tool_dispatch' ORDER BY decided_at")
    return [dict(r) for r in rows]


async def _seed_prior(conn: Any, key: str, *, reliability: float, p50: int, runs: int) -> None:
    await conn.execute(
        "INSERT INTO memory (layer, content, source, claim_key, functional, structured) "
        "VALUES ('semantic', $2, 'system_observation', $1, true, $3)",
        key, f"{key} learned",
        {"reliability": reliability, "latency_p50_ms": p50, "runs": runs, "certainty": "learned"})


async def test_cold_start_settles_a_trace_and_never_cries_wolf(live_pool: Any) -> None:
    emitted: list = []
    loop = _loop(live_pool, emitted)
    j = _Journal(uuid4())
    async with live_pool.acquire() as conn:
        # no `tool:brand_new` memory exists → cold start (no learned prior)
        await loop._ledger_tool_outcome(conn, j, _Tool("brand_new"), {}, success=True,
                                        duration_ms=50, error_text=None)
        traces = await _tool_traces(conn)
    assert len(traces) == 1
    t = traces[0]
    assert t["mode"] == "act" and t["expected_outcome"] == "success"
    assert t["actual_outcome"] == "success"       # SETTLED — the rollup 'settled' bucket is now non-zero
    assert not any(k == "tool.surprise" for (k, _p) in emitted)   # no prior → never a surprise
    assert any(k == "tool.prediction" for (k, _p) in j.events)    # but always an observable prediction


async def test_reliable_tool_failing_is_a_surprise(live_pool: Any) -> None:
    emitted: list = []
    loop = _loop(live_pool, emitted)
    j = _Journal(uuid4())
    async with live_pool.acquire() as conn:
        await _seed_prior(conn, "tool:curl", reliability=0.95, p50=100, runs=20)
        # execute_command is keyed by the BINARY (curl); a 95%-reliable tool FAILS → surprise 0.95
        await loop._ledger_tool_outcome(
            conn, j, _Tool("execute_command"), {"command": "curl https://x"},
            success=False, duration_ms=120, error_text="boom")
        traces = await _tool_traces(conn)
    assert len(traces) == 1 and traces[0]["actual_outcome"] == "failure"
    surprises = [p for (k, p) in emitted if k == "tool.surprise"]
    assert len(surprises) == 1 and surprises[0]["surprise"] >= 0.5
    assert surprises[0]["tool"] == "execute_command"


async def test_rollup_surfaces_prediction_accuracy(live_pool: Any) -> None:
    # The real "is Sali improving?" number: of the effectful ACTIONS taken, what fraction succeeded.
    from sali.cognitive.decision_trace import DecisionTraceStore

    emitted: list = []
    loop = _loop(live_pool, emitted)
    j = _Journal(uuid4())
    async with live_pool.acquire() as conn:
        await _seed_prior(conn, "tool:curl", reliability=0.9, p50=100, runs=20)
        for cmd, ok in (("curl a", True), ("curl b", True), ("curl c", False)):
            await loop._ledger_tool_outcome(
                conn, j, _Tool("execute_command"), {"command": cmd}, success=ok,
                duration_ms=95, error_text=None if ok else "x")
    rollup = await DecisionTraceStore(live_pool).rollup()
    p = rollup["predictions"]
    assert p["total"] == 3 and p["succeeded"] == 2 and p["failed"] == 1
    assert p["accuracy"] == round(2 / 3, 3)          # settled, real, non-cosmetic
    assert rollup["outcomes"]["settled"] >= 3        # the bucket that was structurally 0 before


async def test_expected_success_within_latency_is_not_a_surprise(live_pool: Any) -> None:
    emitted: list = []
    loop = _loop(live_pool, emitted)
    j = _Journal(uuid4())
    async with live_pool.acquire() as conn:
        await _seed_prior(conn, "tool:git", reliability=0.98, p50=200, runs=30)
        # reliable tool SUCCEEDS within typical latency → surprise ~0.02, no event
        await loop._ledger_tool_outcome(
            conn, j, _Tool("execute_command"), {"command": "git status"},
            success=True, duration_ms=180, error_text=None)
    assert not any(k == "tool.surprise" for (k, _p) in emitted)
