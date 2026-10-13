"""What actually happened, when Almir asks about work that already ended.

Sibling to `verify.claims`: same seam in the turn, same discipline, different table. `check_claims`
settles what Almir ASSERTS about this machine; this fetches what the RECORD says about work Sali did
himself, and puts it in front of him BEFORE he answers.

THE DEFECT THIS EXISTS FOR (2026-09-07 00:30). Almir asked "why the task says failed?". Sali replied
"…something went wrong when step five tried to create or access that directory — possibly a permissions
issue or a race condition…". Every causal clause was invented. The true answer was on disk the whole
time, in the task's own archived record:

    "Task stalled — AI announced completion prematurely while step 5 was still running and step 6 was
     pending. Steps 1-4 are done; step 5 needs finishing and step 6 hasn't been executed yet."

So this is an UNRETRIEVED-data defect, not a missing-data one. Nothing here teaches Sali anything; it
just makes guessing stop being the path of least resistance.

DESIGN RULES, each earned:

* PRESENCE ONLY. When nothing is found, nothing is injected — never an "I looked and found no record"
  paragraph. Silence means NOT LOOKED UP, never NOTHING HAPPENED. (Same lesson as world_state's file
  list: the block that isn't warranted is REMOVED, not relabelled.)
* FAIL SILENT over fail wrong. A confident answer about the wrong task is worse than no block at all,
  so every ambiguity resolves to None. Ambiguity is never broken by recency.
* NO INSTRUCTION. The block states facts and stops. No "you should", no steer.
* AGE TWICE, PAST TENSE ALWAYS. Every sentence carries when it ended, relative and absolute, because
  the one way this becomes a new hallucination is an old failure read as current state.
"""

from __future__ import annotations

import contextlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sali.obs.log import get_logger
from sali.verify.claims import sentences

log = get_logger("sali.verify.work_record")

# A question about past work is short. Anything longer is a paste, and scanning it is waste.
_MAX_TEXT = 600

# THE ONE UNCONDITIONAL SCAN. On a turn that asks nothing about past work — 30 of the last 31 real
# turns — this is the entire cost of the module: measured 0.003 ms. (For scale, the neighbouring
# claim probe measures 0.023 ms on the same input.) Everything below is lazy behind it.
_TRIGGER = re.compile(
    r"\b(?:fail|fails|failed|failing|failure|wrong|happen|happened|finish|finished|"
    r"complete|completed|abandon|abandoned|stall|stalled|stopped|status)\b", re.IGNORECASE)

_LEAD = re.compile(r"^(?:sali|hey|ok|okay|so|and|but|um|uh|please)[,\s]+", re.IGNORECASE)
_FUTURE = re.compile(r"\b(?:will|shall|gonna|going\s+to)\b", re.IGNORECASE)
_POLITE = re.compile(r"^(?:can|could|would)\s+you\s+(?:please\s+)?"
                     r"(?:tell\s+me\s+|check\s+|see\s+)?", re.IGNORECASE)
_REQUEST = re.compile(r"^(?:make|change|update|fix|add|remove|run|do|start|redo|retry|try|"
                      r"continue|resume|finish|complete|build)\b", re.IGNORECASE)
_TASK = re.compile(r"\b(?:task|job)\b", re.IGNORECASE)
_YOU_DID = re.compile(r"\b(?:did|have)\s+you\s+(?:actually\s+|ever\s+|already\s+)?"
                      r"(?:finish|finished|complete|completed|do|done|build|built|make|made)\b",
                      re.IGNORECASE)
_TRAIL_Q = re.compile(r"\b(?:why|what|how)\s*[?.!]*$", re.IGNORECASE)
_OPEN = frozenset({"what", "which", "who", "where", "when", "why", "how", "is", "are", "was",
                   "were", "do", "does", "did", "can", "could", "would", "should", "has",
                   "have", "had"})

# DENY BEATS ALLOW. "why did that command fail?" and "sali why ssh not working?" are about a shell
# command and a service — a technically-true paragraph about the wrong subject is worse than silence.
_DENY = frozenset({
    "ssh", "wifi", "internet", "connection", "command", "file", "download", "upload", "docker",
    "git", "github", "build", "builds", "ci", "pipeline", "service", "daemon", "process",
    "laptop", "pc", "browser", "terminal", "shell", "script", "test", "tests",
})

_STOP = frozenset({
    "the", "a", "an", "and", "or", "of", "to", "for", "with", "on", "in", "at", "by", "it",
    "that", "this", "my", "your", "you", "i", "we", "did", "do", "does", "was", "were", "is",
    "are", "why", "what", "how", "when", "happened", "happen", "failed", "fail", "finish",
    "finished", "task", "job", "sali", "about", "from", "please", "tell", "me",
})

_TERMINAL = ("done", "failed", "abandoned", "cancelled", "superseded")
_ARCHIVE = Path.home() / "Desktop" / "sali-works" / "tasks"

DEICTIC = "deictic"
NAMED = "named"


@dataclass(slots=True)
class WorkRecord:
    """One task that already ended, assembled from whichever tier still holds it."""
    task_id: str
    objective: str
    status: str
    ended_at: datetime
    age_s: float
    kind: str                       # deictic | named
    source: str                     # task_row | event_log
    reason: str | None = None
    reason_source: str | None = None    # record | archive | chat
    done: int | None = None
    total: int | None = None
    first_unfinished: tuple[int, str, str] | None = None
    review_summary: str | None = None
    thin: bool = False
    keywords: set[str] = field(default_factory=set)


# ─────────────────────────────────────────────────────────── the text gate

def _keywords(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", text.lower()) if w not in _STOP}


def classify_question(text: str) -> tuple[str, set[str]] | None:
    """(kind, keywords) when this turn asks about work that already ended, else None. Pure."""
    if not text or len(text) > _MAX_TEXT or not _TRIGGER.search(text):
        return None

    def judge(sentence: str, flagged_question: bool) -> tuple[str, set[str]] | None:
        if _FUTURE.search(sentence):
            return None                       # a prediction has no record to consult
        s = _LEAD.sub("", sentence).strip()
        s = _POLITE.sub("", s).strip()
        if _REQUEST.match(s):
            return None                       # an order, not a question
        first = re.sub(r"^\W+", "", s).split(" ", 1)[0].strip(",.?!'\"").lower().split("'")[0]
        asked = (flagged_question or sentence.rstrip().endswith("?")
                 or first in _OPEN or bool(_TRAIL_Q.search(s)) or bool(_YOU_DID.search(s)))
        if not asked:
            return None
        words = set(re.findall(r"[a-z0-9]+", s.lower()))
        if words & _DENY:
            return None                       # about a command/service/file, not a task
        if _TASK.search(s) or _YOU_DID.search(s):
            return DEICTIC, set()
        named = _keywords(s)
        return (NAMED, named) if named else None

    for sentence, flagged in sentences(text):
        verdict = judge(sentence, flagged)
        if verdict:
            return verdict
    # SECOND PASS: "the task failed. why?" splits the outcome word and the interrogative across two
    # sentences, so neither one qualifies alone. Every other guard still applies to the joined text.
    parts = sentences(text)
    if 2 <= len(parts) <= 3 and any(f or s.rstrip().endswith("?") for s, f in parts):
        return judge(" ".join(s for s, _ in parts), True)
    return None


# ─────────────────────────────────────────────────────────── resolution

async def _candidates(pool: Any, now: datetime, horizon: timedelta) -> list[dict[str, Any]]:
    """Ended tasks inside the horizon. The task row first (it is durable — archiving stamps
    archived_at rather than deleting), then the event log, because DELETE /tasks/{id} is a real
    button in the app and it is exactly what removed the task from the worked example."""
    out: dict[str, dict[str, Any]] = {}
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            # coalesce is required, not cosmetic: completed_at is NULL on a task finished through
            # the reviewer path, so age taken from it alone is wrong.
            "SELECT id::text AS id, objective, status, result, last_review_summary, "
            "       coalesce(completed_at, archived_at, updated_at) AS ended_at "
            "  FROM task "
            " WHERE status = ANY($3) "
            "   AND coalesce(completed_at, archived_at, updated_at) >  $1::timestamptz - $2::interval "
            "   AND coalesce(completed_at, archived_at, updated_at) <= $1::timestamptz "
            " ORDER BY ended_at DESC LIMIT 8",
            now, horizon, list(_TERMINAL))
        for r in rows:
            out[r["id"]] = {**dict(r), "source": "task_row"}
        # ALWAYS, not only when the row query came back empty. The task whose record you most need
        # is the one that FAILED, and a failed task is the likeliest to have been deleted from the
        # app afterwards — so it survives only here. Gating this on "no rows at all" made it
        # invisible the moment any newer task existed: asked "why the task says failed?", the probe
        # answered about the SUCCESSOR that completed fine. One indexed read, ~0.06 ms.
        if True:
            ev = await conn.fetch(
                "SELECT f.subject_id::text AS id, f.created_at AS ended_at, "
                "       f.payload->>'status' AS status, "
                "       (SELECT c.payload->>'objective' FROM event c "
                "         WHERE c.subject_id = f.subject_id AND c.event_type = 'task.created' "
                "         ORDER BY c.seq DESC LIMIT 1) AS objective "
                "  FROM event f "
                " WHERE f.event_type = 'task.finished' "
                "   AND f.created_at >  $1::timestamptz - $2::interval "
                "   AND f.created_at <= $1::timestamptz "
                " ORDER BY f.seq DESC LIMIT 8",
                now, horizon)
            for r in ev:
                if r["id"] and r["id"] not in out:
                    out[r["id"]] = {**dict(r), "result": None, "last_review_summary": None,
                                    "source": "event_log"}
    return sorted(out.values(), key=lambda r: r["ended_at"], reverse=True)


def _pick(cands: list[dict[str, Any]], kind: str, keywords: set[str],
          wants_failure: bool) -> dict[str, Any] | None:
    if not cands:
        return None
    if kind == DEICTIC:
        if wants_failure:
            bad = [c for c in cands if (c.get("status") or "") != "done"]
            if bad:
                return bad[0]
        return cands[0]
    # NAMED: the name IS the disambiguator, so exactly one candidate may match. Two is ambiguous and
    # zero is unrelated; both mean silence. Recency must never break a tie here.
    scored = [c for c in cands if _keywords(str(c.get("objective") or "")) & keywords]
    return scored[0] if len(scored) == 1 else None


async def _detail(pool: Any, rec: WorkRecord) -> None:
    """Fill tally / reason / first-unfinished from whichever tier still has them. Keyed strictly by
    id — the archive directory holds 1200+ entries and must NEVER be listed on a foreground turn."""
    with contextlib.suppress(Exception):
        async with pool.acquire() as conn:
            # NO subject_type filter: agent.message rows carry subject_id with subject_type NULL,
            # and that is the row holding the failure narration.
            rows = await conn.fetch(
                "SELECT event_type, created_at, payload FROM event "
                " WHERE subject_id = $1::uuid "
                "   AND event_type IN ('task.step.completed','agent.message') ORDER BY seq",
                rec.task_id)
            steps = await conn.fetch(
                "SELECT seq, description, status FROM task_step WHERE task_id = $1::uuid "
                " ORDER BY seq", rec.task_id)
        done = total = 0
        chat_reason = None
        for r in rows:
            p = r["payload"] if isinstance(r["payload"], dict) else {}
            if r["event_type"] == "task.step.completed":
                # max(done), never count(*): the same completion is emitted more than once, so
                # counting rows over-reports progress.
                with contextlib.suppress(Exception):
                    done = max(done, int(p.get("done") or 0))
                    total = max(total, int(p.get("total") or 0))
            elif p.get("importance") == "failure" and p.get("text"):
                chat_reason = str(p["text"])
        if total:
            rec.done, rec.total = done, total
        for s in steps:
            if s["status"] not in ("done", "skipped"):
                rec.first_unfinished = (s["seq"], str(s["description"] or ""), str(s["status"]))
                break

        meta, progress = _read_archive(rec.task_id)
        if progress:
            # Consulted even when the tally already came from the event log: once the task row is
            # deleted there are no task_step rows either, and this file is the only place the step
            # DESCRIPTIONS survive. Without it the block can say "4 of 6 finished" but never which
            # one stopped — which is the half of the answer Almir actually asked for.
            with contextlib.suppress(Exception):
                st = progress.get("steps") or []
                if st:
                    if not rec.total:
                        rec.total = len(st)
                        rec.done = sum(1 for x in st if x.get("status") in ("done", "skipped"))
                    if rec.first_unfinished is None:
                        nxt = next((x for x in st if x.get("status") not in ("done", "skipped")), None)
                        if nxt:
                            rec.first_unfinished = (int(nxt.get("seq") or 0),
                                                    str(nxt.get("description") or ""),
                                                    str(nxt.get("status") or ""))
        # Reason, in order of authority. The chat tier is labelled differently in the render because
        # it is prose Sali said, not a field the engine recorded.
        if rec.reason:
            rec.reason_source = "record"
        elif meta and meta.get("result"):
            rec.reason, rec.reason_source = str(meta["result"]), "archive"
        elif chat_reason:
            rec.reason, rec.reason_source = chat_reason, "chat"
        if meta and not rec.objective:
            rec.objective = str(meta.get("objective") or "")
        rec.thin = not rec.reason and not rec.total


def _read_archive(task_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Open this task's archive BY ID. Never scan the directory."""
    out: list[dict[str, Any] | None] = []
    for name in ("meta.json", "progress.json"):
        f = _ARCHIVE / task_id / name
        data = None
        if f.is_file():
            with contextlib.suppress(Exception):
                data = json.loads(f.read_text(encoding="utf-8"))
        out.append(data if isinstance(data, dict) else None)
    return out[0], out[1]


async def check_work_question(pool: Any, text: str, *, now: datetime | None = None
                              ) -> WorkRecord | None:
    """The probe. Returns a record ONLY when one task is unambiguously the subject; None otherwise,
    and None is the common answer. Never raises."""
    verdict = classify_question(text or "")
    if verdict is None:
        return None
    kind, keywords = verdict
    now = now or datetime.now(UTC)
    horizon = timedelta(hours=24) if kind == DEICTIC else timedelta(days=30)
    try:
        cands = await _candidates(pool, now, horizon)
        # CAUSALITY: a task that ended AFTER the question cannot be its subject. Without this the
        # probe would answer a question about the failed task by narrating its successful successor.
        cands = [c for c in cands if c["ended_at"] <= now]
        wants_failure = bool(re.search(r"\b(fail\w*|wrong|stall\w*|stopped|abandon\w*)\b",
                                       text, re.IGNORECASE))
        chosen = _pick(cands, kind, keywords, wants_failure)
        if chosen is None or (chosen.get("status") or "") not in _TERMINAL:
            return None
        ended = chosen["ended_at"]
        rec = WorkRecord(
            task_id=chosen["id"], objective=str(chosen.get("objective") or ""),
            status=str(chosen["status"]), ended_at=ended,
            age_s=max(0.0, (now - ended).total_seconds()), kind=kind,
            source=str(chosen.get("source") or "task_row"),
            reason=(str(chosen["result"]) if chosen.get("result") else None),
            review_summary=(str(chosen["last_review_summary"])
                            if chosen.get("last_review_summary") else None),
            keywords=keywords)
        await _detail(pool, rec)
        if not rec.objective:
            return None                       # nothing identifiable to show
        return rec
    except Exception as exc:  # noqa: BLE001 — fail open; the turn proceeds exactly as before
        log.debug("work_record_failed", error=str(exc)[:120])
        return None


# ─────────────────────────────────────────────────────────── render

def render(rec: WorkRecord, *, ago: Any = None) -> str:
    """The block. Facts and a full stop — no instruction, no steer, and the age in every reading."""
    # asyncpg hands back timestamptz in UTC, so a naive strftime printed 22:55 for something that
    # happened at 18:55 — a four-hour error in the one field whose whole job is anchoring the age.
    local = rec.ended_at.astimezone()
    when = local.strftime("%H:%M on %A %-d %B")
    rel = None
    if ago is not None:
        with contextlib.suppress(Exception):
            rel = ago(rec.ended_at)
    if not rel:
        h = rec.age_s / 3600.0
        rel = (f"{int(rec.age_s // 60)} minutes ago" if h < 1
               else f"{int(h)} hours ago" if h < 48 else f"{int(h // 24)} days ago")

    lines = ["A TASK THAT ALREADY ENDED, from its own record. This is history, not current state:",
             f'- "{rec.objective[:200]}"',
             f"- It ended {rec.status.upper()} {rel}, at {when}."]

    if rec.status in ("abandoned", "cancelled"):
        return "\n".join(lines)               # nothing honest to add; most carry no result at all

    if rec.total:
        if rec.done == rec.total:
            lines.append(f"- All {rec.total} of its steps finished.")
        else:
            line = f"- {rec.done} of its {rec.total} steps finished."
            if rec.first_unfinished:
                seq, desc, st = rec.first_unfinished
                line += f' The first that did not was step {seq}, "{desc[:110]}", left {st}.'
            lines.append(line)
    if rec.thin:
        lines.append("- Its detailed record is gone — the task row was deleted and no archive "
                     "folder remains. The event-log entry above is all that survives.")
    if rec.reason:
        label = ("What you told Almir at the time" if rec.reason_source == "chat"
                 else "What it recorded when it stopped")
        lines.append(f'- {label}: "{" ".join(rec.reason.split())[:240]}"')
    if rec.review_summary and rec.status == "done":
        lines.append(f"- The reviewer passed it: {rec.review_summary[:120]}.")
    return "\n".join(lines)


__all__ = ["DEICTIC", "NAMED", "WorkRecord", "check_work_question", "classify_question", "render"]
