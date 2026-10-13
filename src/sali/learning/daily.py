"""Daily consolidation (Prompt 7 §9/§10/§26/§27) — the scheduled "organise what you learned" cycle.

Once a day, deterministically: collect the day's evidence, promote candidates that cleared the evidence
bar, retire failed experiments, surface contradictions, and emit a summary. It REUSES the existing
``LearningService.consolidate`` (procedures / failures / tool-experience / episodes) rather than
re-implementing it, and adds the task-graph layer (candidate promotion, archival) on top (§44).

Guarantees:
  • Idempotent (§26): one cycle per calendar day, claimed via ``consolidation_run(ran_on) UNIQUE``. A
    re-run — or a run after a restart — is a clean no-op, never a duplicate.
  • Bounded context (§10): every query is LIMITed; the only model calls are the already-bounded ones
    inside ``LearningService``. No raw-history dump is ever sent to the model.
  • Bounded catch-up (§26): missed days are found deterministically and at most ``max_cycles`` are run.
  • Background (§27): it takes no execution lease and holds no foreground resource — normal interaction
    stays dominant. It influences future decisions; it never touches a core invariant (§37).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sali.core.enums import MemorySource, compare_sources
from sali.core.knowledge import looks_checkable
from sali.learning.behavior import BehaviorStore
from sali.learning.candidates import LearningCandidateStore
from sali.memory.writer import _record_memory_contradiction
from sali.obs.log import get_logger

log = get_logger("sali.learning.daily")

# A failed experiment that never once succeeded and keeps failing is noise, not knowledge (§5/§13).
_ARCHIVE_AFTER_DAYS = 14
_ARCHIVE_MIN_FAILURES = 3

# FREE-TEXT semantic reconciliation (§13/§46) — the deliberately conservative half. Similarity is only a
# FINDER: the >=0.92 band is surfaced for review; a row is only RETIRED at near-paraphrase (>=0.95) with a
# STRICT evidence-authority win (never a tie), same scope, and NON-verifiable content (a checkable clash is
# sent to grounding, not guessed). Supersession is the SOFT bitemporal close (valid_until + superseded_by),
# so it is reversible + audited, never a delete. Episodic/procedural/experience/identity are excluded.
_RECONCILE_SIM_FLAG = 0.92
_RECONCILE_SIM_RETIRE = 0.95
_RECONCILE_SCAN_LIMIT = 60
_RECONCILE_MAX_RETIRE = 25


@dataclass(slots=True)
class DailySummary:
    ran_on: str
    skipped: bool = False                 # True when the day was already consolidated (idempotent)
    promoted: int = 0
    archived: int = 0
    contradictions_open: int = 0
    candidates_active: int = 0
    behavior_pending: int = 0
    reconciled: int = 0                   # free-text semantic memories superseded (soft, reversible)
    conflicts_flagged: int = 0            # near-duplicate/verifiable conflicts surfaced for review
    consolidated: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DailyConsolidation:
    def __init__(
        self, pool: Any, provider: Any = None, publisher: Any = None, *,
        learning_service: Any = None,
    ) -> None:
        self._pool = pool
        self._provider = provider
        self._publisher = publisher
        self._service = learning_service
        self._candidates = LearningCandidateStore(pool, publisher)
        self._behavior = BehaviorStore(pool, publisher)

    async def run(self, *, on_date: date | None = None) -> DailySummary:
        """One consolidation cycle for ``on_date`` (default: today, UTC). Idempotent — if the day is
        already claimed (by an earlier run, another process, or before a restart), returns a skipped
        summary and does nothing."""
        day = on_date or datetime.now(UTC).date()
        run_id = await self._claim(day)
        if run_id is None:
            return DailySummary(ran_on=day.isoformat(), skipped=True)  # already done today (§26)

        await self._emit("learning.daily_started", {"ran_on": day.isoformat()})
        summary = DailySummary(ran_on=day.isoformat())
        try:
            # 1) reuse the run-based consolidation (procedures/failures/tool-experience/episodes) (§44)
            if self._service is not None:
                res = await self._service.consolidate()
                summary.consolidated = {
                    "procedures": len(getattr(res, "procedures", []) or []),
                    "failures_recorded": getattr(res, "failures_recorded", 0),
                    "episodes_created": getattr(res, "episodes_created", 0),
                    "tool_experiences": getattr(res, "tool_experiences", 0)}

            # 2) task-graph layer: promote what cleared the evidence bar, retire failed experiments (§21/§5)
            for cand in await self._candidates.promotable():
                if await self._candidates.promote(cand):
                    summary.promoted += 1
            summary.archived = await self._archive_stale()

            # 2b) free-text semantic reconciliation — conservative + reversible (see module note)
            summary.reconciled, summary.conflicts_flagged = await self._reconcile_semantic()

            # 3) surface health (bounded reads) for the summary + the iPhone controller (§28)
            counts = await self._candidates.counts()
            summary.candidates_active = counts["active"]
            summary.contradictions_open = counts["open_contradictions"]
            summary.behavior_pending = (await self._behavior.counts())["pending"]
        except Exception as exc:  # noqa: BLE001 - a bad cycle is recorded, never crashes the daemon
            await self._fail(run_id, str(exc))
            await self._emit("learning.daily_failed", {"ran_on": day.isoformat(), "error": str(exc)[:200]})
            log.warning("daily_consolidation_failed", error=str(exc))
            raise

        await self._complete(run_id, summary)
        await self._emit("learning.daily_completed", {"ran_on": day.isoformat(), **summary.to_dict()})
        return summary

    async def _reconcile_semantic(self) -> tuple[int, int]:
        """Reconcile contradictory SEMANTIC free-text memories — the gap the functional/claim_key layer
        never covered (memory.forgotten/contradiction = 0 for free-text). Deliberately conservative:
        embedding similarity is only a FINDER; a row is RETIRED only at near-paraphrase, same scope, a
        STRICT authority win, and non-verifiable content — otherwise the pair is merely flagged (or, if
        checkable, marked for grounding so Sali re-observes instead of guessing). Supersession is the SOFT
        bitemporal close (reversible + audited). Bounded per cycle. Returns (retired, flagged)."""
        retired = 0
        flagged = 0
        seen: set[frozenset[str]] = set()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, content, source::text AS source, scope, embedding::text AS vec "
                "FROM memory WHERE layer='semantic'::memory_layer AND valid_until IS NULL "
                "  AND superseded_by IS NULL AND embed_status='done' AND embedding IS NOT NULL "
                "ORDER BY valid_from DESC LIMIT $1", _RECONCILE_SCAN_LIMIT)
            for r in rows:
                if retired >= _RECONCILE_MAX_RETIRE:
                    break
                near = await conn.fetchrow(
                    "SELECT id, content, source::text AS source, "
                    "  1 - (embedding <=> $1::vector) AS sim "
                    "FROM memory WHERE layer='semantic'::memory_layer AND valid_until IS NULL "
                    "  AND superseded_by IS NULL AND embed_status='done' AND embedding IS NOT NULL "
                    "  AND id <> $2 AND scope = $3 "        # SAME scope only — a global vs project fact differ
                    "ORDER BY embedding <=> $1::vector LIMIT 1", r["vec"], r["id"], r["scope"])
                if near is None or float(near["sim"]) < _RECONCILE_SIM_FLAG:
                    continue
                pair = frozenset((str(r["id"]), str(near["id"])))
                if pair in seen:
                    continue
                seen.add(pair)
                if r["content"] == near["content"]:
                    continue  # identical text is corroboration, not a conflict
                sim = float(near["sim"])
                # VERIFIABLE clash → never guess a winner; flag both for the grounding faculty to settle.
                if looks_checkable(r["content"]) or looks_checkable(near["content"]):
                    await conn.execute(
                        "UPDATE memory SET needs_grounding=true, updated_at=now() "
                        "WHERE id = ANY($1::uuid[]) AND needs_grounding=false", [r["id"], near["id"]])
                    await self._flag_conflict(conn, r["id"], near["id"], sim, "verifiable")
                    flagged += 1
                    continue
                src_r, src_n = MemorySource(r["source"]), MemorySource(near["source"])
                decision = compare_sources(src_r, src_n)  # "new" => r out-ranks near
                # Only a NEAR-PARAPHRASE with a STRICT authority win is retired; ties + the softer band flag.
                if sim < _RECONCILE_SIM_RETIRE or decision == "tie":
                    await self._flag_conflict(conn, r["id"], near["id"], sim,
                                              "tie" if decision == "tie" else "near")
                    flagged += 1
                    continue
                winner, loser = (r, near) if decision == "new" else (near, r)
                w_src, l_src = (src_r, src_n) if decision == "new" else (src_n, src_r)
                async with conn.transaction():
                    closed = await conn.execute(
                        "UPDATE memory SET valid_until=now(), updated_at=now() "
                        "WHERE id=$1 AND valid_until IS NULL AND superseded_by IS NULL", loser["id"])
                    if closed.split()[-1] == "0":
                        continue  # already closed by someone else
                    await conn.execute("UPDATE memory SET superseded_by=$2 WHERE id=$1",
                                       loser["id"], winner["id"])
                    await _record_memory_contradiction(
                        conn, loser["id"], winner["id"], l_src, w_src, "new_wins")
                retired += 1
        if retired or flagged:
            await self._emit("learning.reconciled", {"retired": retired, "flagged": flagged})
        return retired, flagged

    async def _flag_conflict(self, conn: Any, a_id: Any, b_id: Any, sim: float, why: str) -> None:
        await conn.execute(
            "INSERT INTO event (event_type, subject_type, subject_id, payload) "
            "VALUES ('memory.possible_conflict','memory',$1,$2)",
            a_id, {"similar_to": str(b_id), "similarity": round(sim, 3), "reason": why})

    async def catch_up(self, *, max_cycles: int = 1) -> list[DailySummary]:
        """Run missed cycles, most-recent first, up to ``max_cycles`` (§26). If the machine was offline
        for a week, this does NOT run seven cycles — it runs a bounded number and moves on. Always
        includes today. Deterministic: only days with no consolidation_run are candidates."""
        today = datetime.now(UTC).date()
        last = await self._last_run_date()
        start = (last + timedelta(days=1)) if last else today
        missed = [today - timedelta(days=i) for i in range((today - start).days + 1)] if start <= today \
            else [today]
        missed = sorted(set(missed), reverse=True)[:max(1, max_cycles)]
        out: list[DailySummary] = []
        for day in missed:
            out.append(await self.run(on_date=day))
        return out

    async def _archive_stale(self) -> int:
        """Retire candidates that are old, never succeeded, and keep failing — failed experiments are
        not knowledge (§5). They are marked 'rejected' (recorded, auditable), never deleted."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "UPDATE learning_candidate SET verification_state='rejected', updated_at=now() "
                "WHERE verification_state IN ('observed','attempted') AND promoted=false "
                "  AND times_successful=0 AND times_failed >= $1 "
                "  AND created_at < now() - make_interval(days => $2) RETURNING id",
                _ARCHIVE_MIN_FAILURES, _ARCHIVE_AFTER_DAYS)
        for r in rows:
            await self._emit("learning.rejected",
                             {"candidate_id": str(r["id"]), "reason": "stale failed experiment"})
        return len(rows)

    # ── the idempotent day-claim ledger (§26) ───────────────────────────────────────────────────────
    async def _claim(self, day: date) -> Any:
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "INSERT INTO consolidation_run (ran_on, status) VALUES ($1, 'running') "
                "ON CONFLICT (ran_on) DO NOTHING RETURNING id", day)

    async def _complete(self, run_id: Any, summary: DailySummary) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE consolidation_run SET status='completed', summary=$2, completed_at=now() "
                "WHERE id=$1", run_id, summary.to_dict())

    async def _fail(self, run_id: Any, error: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE consolidation_run SET status='failed', summary=$2, completed_at=now() "
                "WHERE id=$1", run_id, {"error": error[:400]})

    async def _last_run_date(self) -> date | None:
        async with self._pool.acquire() as conn:
            val: date | None = await conn.fetchval(
                "SELECT max(ran_on) FROM consolidation_run WHERE status='completed'")
        return val

    async def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        import contextlib
        with contextlib.suppress(Exception):
            await self._publisher.emit(event_type=event_type, subject_type="learning",
                                       origin="background", data=data)
