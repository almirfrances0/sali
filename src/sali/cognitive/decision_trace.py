"""DecisionTraceStore — the compact structured metadata for every autonomous decision (§29/§44).

The InitiativeDriver, CommunicationDecisionEngine, and any other autonomy source writes a row
per meaningful decision. This is NOT chain-of-thought (deliberately not stored). It IS the
operational reasoning metadata Almir can query to answer "why did Sali do X at 4pm today?"
and the rollup that lets §37 measure "is Sali actually improving?"

Each row carries: mode (from the §10 discrete set), subject_ref (what it was about), origin
(which driver made it), reason_codes, confidence, evidence dict, expected_outcome, and
(populated by a follow-up call once observed) actual_outcome + outcome_at + learning_id.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sali.obs.log import get_logger

log = get_logger("sali.cognitive.decision_trace")


VALID_MODES = ("act", "communicate", "learn", "observe", "wait",
               "defer", "ask", "abort")


class DecisionTraceStore:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def record(
        self, *, mode: str, subject_ref: str | None = None,
        origin: str = "initiative_engine",
        reason_codes: list[str] | None = None,
        confidence: float = 0.5,
        evidence: dict[str, Any] | None = None,
        expected_outcome: str | None = None,
        policy_result: str = "accepted",
    ) -> UUID:
        """Record a decision — always succeeds silently on missing-table (test fixture) to keep
        the caller's loop robust. Returns the trace id so a later `observe_outcome()` can
        settle it."""
        if mode not in VALID_MODES:
            raise ValueError(f"unknown mode: {mode}")
        trace_id = uuid4()
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO sali.decision_trace "
                    "(id, mode, subject_ref, origin, reason_codes, confidence, evidence, "
                    " expected_outcome, policy_result) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
                    trace_id, mode, subject_ref, origin,
                    ",".join(reason_codes or [])[:500],
                    max(0.0, min(1.0, float(confidence))),
                    evidence or {}, expected_outcome, policy_result)
        except Exception as exc:  # noqa: BLE001 — missing table / DB blip → don't break the loop
            log.debug("decision_trace_record_skipped", error=str(exc))
        return trace_id

    async def observe_outcome(
        self, trace_id: UUID, *, actual_outcome: str,
        learning_id: UUID | None = None,
    ) -> None:
        """Settle a prior decision with what actually happened. Called by whatever observed the
        world's response (a completed initiative attempt, a user reply, a resource change). If
        a learning event fired as a result, its id is linked back for the §36/§37 rollup."""
        with contextlib.suppress(Exception):
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE sali.decision_trace "
                    "SET actual_outcome = $2, outcome_at = now(), learning_id = $3 "
                    "WHERE id = $1", trace_id, actual_outcome[:200], learning_id)

    async def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT id, mode, subject_ref, origin, reason_codes, confidence, "
                    "evidence, expected_outcome, actual_outcome, outcome_at, policy_result, "
                    "learning_id, decided_at FROM sali.decision_trace "
                    "ORDER BY decided_at DESC LIMIT $1", min(max(1, limit), 500))
        except Exception:
            return []
        return [dict(r) for r in rows]

    async def rollup(self, since: datetime | None = None) -> dict[str, Any]:
        """Aggregate by mode + outcome-status for the metrics endpoint. Cheap query, no
        chain-of-thought — just counts."""
        try:
            async with self._pool.acquire() as conn:
                if since is not None:
                    mode_rows = await conn.fetch(
                        "SELECT mode, count(*) AS n FROM sali.decision_trace "
                        "WHERE decided_at >= $1 GROUP BY mode ORDER BY n DESC", since)
                    outcome_rows = await conn.fetch(
                        "SELECT "
                        "  count(*) FILTER (WHERE actual_outcome IS NOT NULL) AS settled, "
                        "  count(*) FILTER (WHERE actual_outcome IS NULL) AS pending, "
                        "  count(*) FILTER (WHERE policy_result = 'refused') AS refused "
                        "FROM sali.decision_trace WHERE decided_at >= $1", since)
                    pred_rows = await conn.fetch(
                        "SELECT count(*) AS total, "
                        "  count(*) FILTER (WHERE actual_outcome='success') AS succeeded, "
                        "  count(*) FILTER (WHERE actual_outcome='failure') AS failed, "
                        "  count(*) FILTER (WHERE (evidence->>'surprise')::float >= 0.5) AS surprises, "
                        "  round(avg((evidence->>'surprise')::float)::numeric, 3) AS mean_surprise "
                        "FROM sali.decision_trace WHERE origin='tool_dispatch' AND decided_at >= $1", since)
                else:
                    mode_rows = await conn.fetch(
                        "SELECT mode, count(*) AS n FROM sali.decision_trace "
                        "GROUP BY mode ORDER BY n DESC")
                    outcome_rows = await conn.fetch(
                        "SELECT "
                        "  count(*) FILTER (WHERE actual_outcome IS NOT NULL) AS settled, "
                        "  count(*) FILTER (WHERE actual_outcome IS NULL) AS pending, "
                        "  count(*) FILTER (WHERE policy_result = 'refused') AS refused "
                        "FROM sali.decision_trace")
                    pred_rows = await conn.fetch(
                        "SELECT count(*) AS total, "
                        "  count(*) FILTER (WHERE actual_outcome='success') AS succeeded, "
                        "  count(*) FILTER (WHERE actual_outcome='failure') AS failed, "
                        "  count(*) FILTER (WHERE (evidence->>'surprise')::float >= 0.5) AS surprises, "
                        "  round(avg((evidence->>'surprise')::float)::numeric, 3) AS mean_surprise "
                        "FROM sali.decision_trace WHERE origin='tool_dispatch'")
        except Exception:
            return {"by_mode": {}, "outcomes": {}, "predictions": {}}
        by_mode = {r["mode"]: int(r["n"]) for r in mode_rows}
        outcomes = dict(outcome_rows[0]) if outcome_rows else {}
        # PREDICTION signal (the real "is Sali improving?" number): of the effectful tool ACTIONS Sali
        # took, how many succeeded as expected, and how often a confident expectation was surprised.
        # Rising accuracy + falling mean_surprise = better-calibrated, more reliable action over time.
        preds = dict(pred_rows[0]) if pred_rows else {}
        total_p = int(preds.get("total") or 0)
        succeeded_p = int(preds.get("succeeded") or 0)
        predictions = {
            "total": total_p, "succeeded": succeeded_p, "failed": int(preds.get("failed") or 0),
            "surprises": int(preds.get("surprises") or 0),
            "mean_surprise": float(preds["mean_surprise"]) if preds.get("mean_surprise") is not None else None,
            "accuracy": round(succeeded_p / total_p, 3) if total_p else None,
        }
        return {"by_mode": by_mode,
                "outcomes": {k: int(v or 0) for k, v in outcomes.items()},
                "predictions": predictions}
