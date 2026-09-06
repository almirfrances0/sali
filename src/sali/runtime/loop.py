"""The agent execution loop — the explicit FSM at Sali's core.

INPUT → RETRIEVE → BUILD_CONTEXT → REASON_PLAN → [AWAIT_CONFIRM → EXECUTE_TOOL → OBSERVE →
VERIFY → UPDATE_STATE]* → LEARN → RESPOND. The LLM is called at exactly one hot-loop site
(REASON_PLAN); retrieve, dispatch, verify, and journaling are deterministic. Every tool
effect is journaled ``executing`` *before* it runs (fix M15) and verified after — a tool is
never assumed to have succeeded (rule 13).
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import hashlib
import json
import re
import time
from datetime import timedelta
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sali.browser.service import build_browser
from sali.comms.service import CommsService
from sali.config.secrets import SecretStore
from sali.config.settings import Settings
from sali.context.engine import LIVE_NOTE, ContextEngine
from sali.core.clock import Clock, SystemClock
from sali.core.enums import MemoryLayer, MemorySource, RiskLevel
from sali.core.errors import ProviderError
from sali.provider.presets import BALANCED, CREATIVE
from sali.core.ids import new_id
from sali.core.knowledge import classify_knowledge, epistemic_status, looks_checkable
from sali.core.temporal import TemporalService
from sali.core.scope import project_scope
from sali.core.toolvocab import binary_of
from sali.graph.service import GraphService
from sali.ingest.service import IngestService
from sali.learning.episodes import prune_stm
from sali.memory.writer import observe
from sali.obs.log import get_logger
from sali.perception.service import build_perception
from sali.provider.base import ChatMessage, ChatResult, ModelProvider, ToolCall
from sali.retrieval.router import classify
from sali.retrieval.service import RetrievalService
from sali.runtime import context_budget, continuation, pending
from sali.runtime.health import HealthService
from sali.runtime.journal import RunJournal
from sali.runtime.referential import extract_proposed_command, is_delegation, is_pure_social
from sali.runtime.self_state import SelfStateStore
from sali.runtime.state import ResumeAction, RunState, resume_action
from sali.runtime.world_state import WorldStateBuilder
from sali.scheduler.store import ScheduleStore
from sali.security.confirm import Confirmer
from sali.security.policy import Action, PolicyDecision, PolicyEngine
from sali.security.redact import redact_obj
from sali.tasks.authority import TaskAuthority
from sali.tasks.logger import append_event
from sali.tasks.store import TaskStore
from sali.tools import dispatch
from sali.tools.base import VerifyResult
from sali.tools.context import ToolContext
from sali.tools.registry import ToolRegistry
from sali.tools.remote import build_remote_runner
from sali.twin import awareness as twin_awareness
from sali.verify.claims import check_claims
from sali.verify.claims import render as render_checks
from sali.verify.engine import verify_effect

# Tools whose arguments carry a raw shell command — the binary they run is what the tool-authority
# floor is keyed on.
_COMMAND_TOOLS = frozenset({"execute_command", "ssh_run"})
_AUTHORITY_TTL = 300.0  # seconds — refresh the system-critical binary set at most this often


def _command_binary(tool_name: str, args: dict[str, Any]) -> str | None:
    if tool_name not in _COMMAND_TOOLS:
        return None
    cmd = args.get("command")
    return binary_of(cmd) if isinstance(cmd, str) and cmd else None


def _retrieval_trace(bundle: Any) -> dict[str, Any]:
    """Why each memory was selected (§7): candidate count, which retriever found it, and the ranking
    signals (score/similarity/confidence/freshness) + epistemic kind of the top hits. Journaled for
    diagnostics; never shown to Almir by default."""
    hits = bundle.memories
    by_retriever: dict[str, int] = {}
    for h in hits:
        by_retriever[h.retriever] = by_retriever.get(h.retriever, 0) + 1
    top: list[dict[str, Any]] = []
    for h in hits[:5]:
        m = h.memory
        ktype = classify_knowledge(m.source, m.layer, needs_grounding=m.needs_grounding,
                                   confidence=h.effective_confidence)
        top.append({
            "content": m.content[:60], "retriever": h.retriever, "score": round(h.score, 3),
            "similarity": round(h.similarity, 3) if h.similarity is not None else None,
            "confidence": round(h.effective_confidence, 2), "freshness": round(h.freshness_factor, 2),
            "stale": h.stale, "knowledge_type": ktype.value,
        })
    return {
        "candidates": len(hits), "by_retriever": by_retriever,
        "graph_contribution": len(bundle.graph_facts),
        "experience_contribution": len(bundle.procedures) + len(bundle.experiences),
        "top": top,
    }

# Warmer sampling so Sali sounds like a person, not a deterministic tool. Tool-calling is
# rendered structurally by the model, so it still works reliably at this temperature.
_VOICE: dict[str, Any] = {
    "temperature": 0.7, "top_k": 40, "top_p": 0.95, "presence_penalty": 0.3, "repeat_penalty": 1.1,
    # The repetition penalty has to be able to SEE the thing it is meant to prevent. llama.cpp's default
    # repeat_last_n is 64 tokens; the previous reply that the anti-repeat block quotes sits ~165 tokens
    # upstream of the generation point, so the penalty was looking at a window that never contained it —
    # and Sali re-emitted a 65-character line byte-for-byte one turn later. 512 covers the tail of the
    # prompt where the last reply and the anti-repeat quote both live.
    "repeat_last_n": 512,
}

# A reply that PROMISES an action. Used to decide whether the follow-through judge should look at a turn
# that called no tools: "ok let me actually check tanzahost.com right now" (three times, no tool call
# ever) is exactly the shape that must not be taken at face value.
# A reply claiming the work is ALREADY DONE. This is the more damaging half of the same failure and it
# had no detector at all.
#
# 2026-09-03 06:19, verbatim: Almir asked for a five-page site. Sali answered "Done. Five static HTML
# files using Tailwind via CDN..." and described every page in detail — hero sections, £25/£45/£75 pricing
# tiers, a postcode checker — with `tool_calls: 0`. Nothing existed. Asked "confirm if it's true you did
# that task", it doubled down: "Yes. I created five HTML/Tailwind pages under /home/almir/Desktop/...".
# Almir found no folder.
#
# `_PROMISE_RE` only ever matched the FUTURE ("I'll…", "let me…"), so a past-tense fabrication matched
# neither branch of the follow-through guard and shipped unchallenged. Claiming completion with no tool
# call in the entire turn is not a stall — it is a false statement about the world, and it is the one
# thing that must never reach him unchecked.
_CLAIMED_DONE_RE = re.compile(
    r"\b(done\b|all done|finished\b|completed\b|i (created|built|made|wrote|added|set up|saved|"
    r"generated|updated|installed|configured|deployed|fixed)\b|i'?ve (created|built|made|written|added|"
    r"set up|saved|generated|updated|installed|configured|deployed|fixed)\b|"
    r"(files?|folder|directory|page|pages|script|site) (is|are) (now )?(in|at|under|ready|created)|"
    r"here'?s what (each|i)\b|all (five|four|three|six|set)\b)",
    re.IGNORECASE,
)

# A claim that a STEP moved. Distinct from _CLAIMED_DONE_RE, which is about the work in general: this
# is Sali narrating the task's own bookkeeping — "Step 3 verified", "advancing step 3 to done",
# "marking step 2 complete" — which is checkable against the row itself, and was the exact wording of
# the failure Almir caught: "Step 3 verified — archive is on disk … Advancing step 3 to done." while
# step 3 sat `pending` in the database.
_STEP_CLAIM_RE = re.compile(
    r"\b(?:step\s*#?\s*\d+\s*(?:is\s+|was\s+)?(?:done|complete|completed|verified|finished|passed)"
    r"|(?:advanc\w*|mark\w*|closing|completing)\s+step\s*#?\s*\d+"
    r"|step\s*#?\s*\d+\s*(?:→|->|to)\s*done)",
    re.IGNORECASE,
)

# Genuine promise verbs only. The old union included conversational fillers ("one sec",
# "hold on", "give me a minute", "checking", "on it", "looking it up"). Those are stall
# words for the current turn, not commitments across time - and pairing them with the
# temporal resolver made every "I'll ping you in a minute" a 60-second commitment that
# went overdue immediately, then confabulated into "scheduled checks were overdue" the
# next time Sali looked at his agenda.
_PROMISE_RE = re.compile(
    r"\b(let me\b|i'?ll\b|i will\b|i'?m going to\b|gonna\b|about to\b)",
    re.IGNORECASE,
)

# An OPEN LOOP is a mid-density state: "I noticed X and haven't resolved it yet." Distinct from a
# commitment (has a deadline) and a task (has a plan). These verbs signal "something is now on my
# mind that I owe follow-up on" WITHOUT a deadline. Deliberately narrow — the follow-through judge
# handles same-turn intent; this only fires when the reply defers into future attention.
_OPEN_LOOP_RE = re.compile(
    r"\b("
    r"i'?ll (look into|investigate|check on|come back to|circle back to|revisit|dig into)|"
    r"let me (look into|investigate|check on|dig into)|"
    # thinking-verbs (figure out / understand / verify) removed: "I need to understand X" is almost
    # always the CURRENT turn's reasoning, not a durable follow-up — it filed Sali's own chain-of-
    # thought ("First, I need to understand what's going on") as a promise_followup open loop.
    r"i (need|want) to (look into|investigate|check on|dig into|follow up on)|"
    r"still (need|want) to (look into|investigate|check on|dig into|follow up on)|not sure yet|"
    r"open question|remains to be seen|worth (looking into|investigating)|"
    r"todo:|follow.?up:"
    r")\b",
    re.IGNORECASE,
)
# Planning-narration / vague-subject fillers: phrases _OPEN_LOOP_RE can still catch that are the
# CURRENT turn's reasoning, not something Sali is durably holding open. A step-opener ("First, ...",
# "Let me start ...") or a self-referential subject with nothing concrete to come back to ("what's
# going on", "the current situation") must not become a promise_followup open loop. Guards
# _capture_open_loop (root cause of the "First, I need to understand what's going on" junk rows).
_OPEN_LOOP_FILLER_RE = re.compile(
    r"^(?:first|okay|ok|alright|so|now|to start|let me start|let'?s start|step\s*\d)\b"
    r"|what'?s (?:going on|happening)|the (?:current )?situation|what is going on"
    r"|the current state|figure out what|understand what",
    re.IGNORECASE,
)
# Brain-audit Turn 6: _SUMMARIZE deleted. Call sites now pass preset=BALANCED explicitly.
# The old partial dict merged UNDER DETERMINISTIC, which leaked seed=42 - fixing summaries
# and the judge nudge on the same output every time (a hallucination amplifier).

# Fold older turns into a running summary once this many uncompacted messages accumulate,
# always keeping the most recent few verbatim. Generous — let Sali keep more context alive.
_COMPACT_AFTER = 40
# Compact once the un-summarised tail reaches this fraction of the working context budget, however few
# messages that took. Keeps the window from filling between count-based compactions.
_COMPACT_AT_BUDGET_FRACTION = 0.5
_KEEP_RECENT = 10

# Intra-turn context folding: when the *working* prompt for a single task nears the model's window,
# fold everything done so far into a compact progress note and carry on — so a long, many-step job
# continues on its own instead of dead-ending on the context limit. The budget/thresholds now live in
# runtime.context_budget (provider-derived window, output reservation, safe→emergency status ladder).
# A context overflow is recovered a bounded number of times per turn: fold hard, then retry (§8/§29).
_MAX_OVERFLOW_RECOVERIES = 3
# Follow-through: models sometimes *narrate* an action ("I'll run these in parallel") and then
# stop without calling anything, leaving Almir to re-prompt. When a reply defers a machine
# action but emits no tool call, we nudge Sali to actually do it — bounded, so it can't loop.
# The model occasionally emits a malformed tool call the backend can't parse (a transient 500).
# Re-sampling at the warm voice temperature almost always yields a clean one, so retry a couple
# of times before giving up rather than crashing the whole turn.
_MAX_PROVIDER_RETRIES = 3

# Enough pushes to carry a large multi-step build (many files, tests, deploy) to completion,
# bounded so it can never spin. With 65K ctx, Sali can hold more steps in mind at once.
_MAX_FOLLOW_THROUGH = 8
# Research delivery (Almir §): only a substantial research answer earns a summary+downloadable report;
# below this the chat answer is already short enough to stand alone.
_RESEARCH_REPORT_MIN_CHARS = 500
# Sectioned deep research (Almir §): a request that asks for depth ("detailed report", "10 pages")
# grows a real multi-page document — outline, then write each section and APPEND it to the file — while
# the chat still shows one small summary. Hard-bounded so it stays minutes, never hours: a section cap
# and a wall-clock budget, whichever comes first.
_MAX_RESEARCH_SECTIONS = 8
_RESEARCH_TIME_BUDGET_S = 240.0
# Below MIN_CHARS a research answer is small — it stays in the chat with NO file (Almir: "some no even
# put on file just in chat if it's just a small one"). At/above SECTION_TRIGGER (or when depth is asked
# for) Sali is asked to OUTLINE the report — and Sali decides how deep it goes, right down to "NONE"
# (Almir: "it's according to sali how he sees it and what he collected"). In between, the overview is
# filed as-is (a short report) with no sectioning.
_RESEARCH_SECTION_TRIGGER_CHARS = 1200
_OUTLINE_SYS = (
    "You decide how deep this report should go, based ONLY on the topic and the overview already written "
    "— how much genuinely useful material there is. If the overview already covers it well, or the topic "
    "is small, output exactly the single word: NONE. Otherwise output the section titles that would "
    "complete a thorough report — as MANY or as FEW as the material warrants, up to {n} — each a short "
    "title (3 to 7 words) covering a DISTINCT aspect the overview did not already cover. One title per "
    "line: titles only, no numbering, no markdown, no preamble.")
_SECTION_SYS = (
    "You are writing ONE section of a thorough report on the given topic. Write the section with the "
    "given title: concrete, specific, well-organised prose (short sub-points where they help). 200 to "
    "450 words. Do NOT repeat the title as a heading and do NOT open with filler like 'in this section' "
    "— just the content. If you are not sure of a fact, say so rather than inventing it.")
_SUMMARISE_SYS = (
    "Summarise the research below for a chat reply to Almir: 3 to 5 tight sentences of the key takeaways "
    "only. No headings, no bullet lists, no preamble like 'here is a summary' — just the gist, in a "
    "plain, direct voice. The full detail is saved separately for download, so do NOT try to include "
    "everything; give him the highlights.")
# When the turn ran NO tools and the reply claims work was done, "you haven't finished" is the wrong
# thing to say — nothing was started. The general nudge even ends with "then tell me it's done", which is
# exactly the sentence that produced the fabrication. This one names what happened.
_FABRICATION_NUDGE = (
    "(STOP. You called no tools this turn, so nothing you just described actually happened — no file was "
    "written, no folder was made, nothing exists. Do not repeat that claim. Either do the work now, in "
    "this reply, with real tool calls — and for anything needing more than a couple of steps, call "
    "plan_task first so it runs as a real task he can watch — or tell him plainly that you have not done "
    "it yet and what you need. Describing work you did not do is the one thing you must never do.)"
)

# How many reasoning cycles one task-continuation turn may take. Sized for a single step that needs a
# few tools — search, fetch, write, check, mark — with headroom, and NOT for a whole plan.
_TASK_TURN_MAX_ITER = 16

_MARK_STEP_NUDGE = (
    "(You did work on this task but marked no step. Call advance_task now for the step you actually "
    "worked — the FIRST unfinished one — with status 'done' if it is finished, 'failed' with a note if "
    "it is not, or 'skipped' if it genuinely was not needed. Almir watches these land; work that is not "
    "marked looks to him like nothing happened.)"
)

_PLAN_FIRST_NUDGE = (
    "(This is coding work and you have not created a task for it. Call plan_task now — the objective and "
    "the real steps — before doing any more of it. Almir asked for this explicitly: coding jobs run as "
    "tasks, with steps he can watch and a reviewer at the end, not inline in a reply.)"
)

# Rewritten (step-discipline): the OLD text said "Do ALL the remaining steps NOW... Don't stop between
# steps" — the single instruction most responsible for Sali running ahead of his own plan (marking step 1
# while doing 2 and 3). It now asks him to finish the ONE thing he started with real tool calls, or say
# what's blocking — never to blast the whole task through one reply. Multi-step work belongs in a task,
# driven one step at a time (see _PLAN_FIRST_NUDGE / the continuation driver).
_FOLLOW_THROUGH_NUDGE = (
    "(You started this but didn't finish it. Finish THIS ONE thing now with real tool calls — do not "
    "describe it, do it — or tell me plainly what's blocking you. Don't try to do everything at once.)"
)

# For a task-continuation turn (driving ONE step): if the step isn't finished, the fix is to finish
# THIS step only and mark it — never to run ahead into the next step. The engine hands the next step
# back on its own turn.
_STEP_INCOMPLETE_NUDGE = (
    "(This step is not finished yet. Do ONLY this step's remaining work now, with real tool calls, "
    "then call advance_task for it. Do NOT start the next step — you'll be handed it automatically on "
    "its own turn. Do not describe the work; do it.)"
)

# Sali sometimes gets stuck investigating — re-reading the same files, re-listing the same dirs —
# instead of acting. When it repeats tool calls it already made, that's a loop; break it.
_LOOP_REPEATS = 3
_HANDOFF_NUDGE = (
    "(You've just planned that task and its steps are stored durably. STOP working on it in this turn — "
    "a background worker picks it up within a minute and works it step by step, which is what keeps "
    "Almir's chat responsive while it runs. Do NOT call any more tools. Reply to him now, briefly and in "
    "your own voice: you're starting it, what it is, and that you'll tell him when it's done.)"
)

_ANTI_LOOP_NUDGE = (
    "(You're repeating tool calls you've already made — you're going in circles. Stop investigating: "
    "make the change or fix directly NOW, or if you're genuinely stuck, stop and tell Almir plainly "
    "what you found and what's blocking you. Don't read or list anything you've already looked at.)"
)
# When the model ends a turn with NOTHING to say (e.g. it gave up after failing tool attempts), never
# leave Almir staring at a blank reply — make it say, in its own voice, what happened or what blocked it.
_EMPTY_WRAP_NUDGE = (
    "(You just returned an empty reply — Almir would see nothing. Tell him plainly, in your own words, "
    "what you found, what you did, or exactly what blocked you and why. Don't call any tools.)"
)
_EMPTY_FALLBACK = (
    "I couldn't finish that — I hit a wall and don't have a clear result to give you. "
    "Want me to try a different way?"
)

# CONFABULATION GUARD (honesty). A "personal recall" question — about shared history, the past, or
# "do you remember" — must be answered from the RECORD, not from what sounds plausible. Observed live
# on a clean slate: asked "what did we work on last week?", Sali invented an entire evening of fake
# debugging (Docker/pgvector/etc). When such a question retrieves NOTHING and there is no conversation
# history to answer from, the turn injects the CHECKED FACT that the record is empty, so the model
# states it has no record instead of fabricating one. Ground truth, not a behavioural nudge.
_PERSONAL_RECALL = re.compile(
    r"\b(we|us|our|you and i|remind me|do you (remember|recall)|did we|were we|had we|"
    r"last (week|month|time|night|year|session)|yesterday|earlier|the other day|a while ago|"
    r"what (did|were|have) (we|you|i)|remember when|back when|previously)\b",
    re.IGNORECASE,
)


def _is_personal_recall(text: str) -> bool:
    return bool(text and len(text) < 600 and _PERSONAL_RECALL.search(text))


_RECALL_GROUNDING = (
    "(Grounding for what he just asked about the past: the automatic memory search found NOTHING for "
    "it. Before you answer, CHECK the real record — your memory tools, the git log, the actual files "
    "— do NOT answer from imagination, and NEVER invent specific past events, dates, or work that you "
    "have not verified. If, after checking, there is genuinely nothing, say plainly that you have no "
    "record of it and ask him to remind you. A truthful 'I don't have that on record' is always "
    "better than a confident description of something that did not happen.)"
)


def _pace_chunks(text: str, *, max_pieces: int = 220) -> list[str]:
    """Split the final answer into small pieces for a live typewriter feel (Almir: "streaming writing
    one by one the output"). Character-based so newlines/markdown are preserved verbatim; the step
    widens for long answers so they type out quickly rather than tediously. Bounded to ~max_pieces
    pieces total, so at ~25ms/piece even a very long answer finishes in a few seconds."""
    n = len(text)
    if n == 0:
        return []
    step = max(1, (n + max_pieces - 1) // max_pieces)
    return [text[i:i + step] for i in range(0, n, step)]


def _scope_block(task: Any, target_path: str) -> int | None:
    """Step-discipline scope guard: "he must never do anything without the step first."

    Given a WRITE target during a task-continuation turn, decide whether that file belongs to a LATER
    step — in which case writing it now is 'doing ahead' and must be blocked. Returns the owning later
    step's seq (>0) to block, -1 to block when the CURRENT step explicitly excludes it but no named
    owner is found, or None to ALLOW.

    Deliberately conservative — it allows unless it can point at a concrete reason to block, so it
    never gets in the way of legitimate current-step work:
      * a file named by the current step's own description/definition_of_done → ALLOW.
      * a file the current step's scope_excludes names → BLOCK (explicitly not this step).
      * a file named by a strictly-later step (and not the current one) → BLOCK, owner = that seq.
      * a file named nowhere → ALLOW (no basis to block).
    """
    if not target_path:
        return None
    try:
        steps = list(getattr(task, "steps", None) or [])
        unfinished = [s for s in steps if getattr(s, "status", "") in ("pending", "running")]
        if not steps or not unfinished:
            return None
        cur_seq = min(int(getattr(s, "seq", 0)) for s in unfinished)
        cur = next((s for s in steps if int(getattr(s, "seq", 0)) == cur_seq), None)
        base = target_path.strip().strip("`\"'").rsplit("/", 1)[-1]
        if len(base) < 5:            # too generic (e.g. "app") to attribute safely
            return None
        cur_own = (f"{getattr(cur, 'description', '') or ''} "
                   f"{getattr(cur, 'definition_of_done', '') or ''}")
        if base in cur_own:          # the current step's own file → always allowed
            return None
        if base in (getattr(cur, "scope_excludes", "") or ""):  # explicitly out of scope
            # try to name the owner; else signal "a later step"
            for s in steps:
                if int(getattr(s, "seq", 0)) > cur_seq and base in (
                        f"{getattr(s, 'description', '') or ''} "
                        f"{getattr(s, 'definition_of_done', '') or ''}"):
                    return int(getattr(s, "seq", 0))
            return -1
        for s in steps:              # named by a strictly-later step → doing ahead
            seq = int(getattr(s, "seq", 0))
            if seq <= cur_seq:
                continue
            if base in (f"{getattr(s, 'description', '') or ''} "
                        f"{getattr(s, 'definition_of_done', '') or ''}"):
                return seq
        return None
    except Exception:  # noqa: BLE001 — a guard must never break the turn; on doubt, ALLOW
        return None
# The SAME (tool, args) call failing identically this many times → stop dispatching it and hand the
# failure back as a factual result, so a wrong call (e.g. a hallucinated path) can't be retried forever.
_MAX_TOOL_FAILURES = 3
# How often the background consolidation pass (§17-19: mine procedures, record failures, distill an
# episode) may run. Throttled so a busy session doesn't distill every turn; it runs OFF-THREAD after
# the turn is already done, so it never adds latency to Almir's reply.
_CONSOLIDATE_EVERY_S = 300.0

# A tool call sometimes leaks out as *text* instead of a parsed call (the model emits the JSON
# itself). We must never leave Almir staring at raw JSON, so we detect it and ask for a clean redo.
_MAX_JSON_RECOVERY = 2
# The API folds a vision description into the turn so the model can actually reason over a photo
# (POST /conversation/message with image_ref). That description is SALI's words, not Almir's — persisting
# it verbatim as his message replaced the photo he sent with a ~2.5k-character wall of text in the app
# transcript (§13/§23: his side must show what HE said). The model still receives the full folded input
# for this turn; only the durable user message is shortened.
_IMAGE_FOLD_RE = re.compile(
    r"\n*\[(?:You looked at an image Almir shared\. What you can see: "
    r"|The user shared an image; it shows: ).*\Z",
    re.DOTALL)


def _self_directed_note(text: str) -> str:
    """What the transcript should show for a turn Sali gave HIMSELF.

    Not empty: the conversation is what later turns read back, so dropping it entirely would make his
    own background work invisible to him. Not verbatim either — that is internal scaffolding, and it
    reads as Almir's words. A one-line description keeps the continuity and loses the machinery."""
    first = " ".join((text or "").split())[:160]
    return f"[my own background check] {first}" if first else "[my own background check]"


def _visible_user_text(text: str) -> str:
    """What Almir's own transcript should show for a turn that carried an image.

    Two things to balance. Persisting the WHOLE vision fold replaced his photo with a ~2.5k-character
    wall of Sali's words in his own transcript. But stripping it entirely costs memory: the durable
    conversation is what later turns read, so Sali would forget what he had been shown. So keep a short,
    honest note — enough to remember by, small enough to read past.
    """
    marker = "[You looked at an image Almir shared. What you can see: "
    legacy = "[The user shared an image; it shows: "
    if marker not in text and legacy not in text:
        return text
    caption = _IMAGE_FOLD_RE.sub("", text).strip()
    gist = ""
    head = marker if marker in text else legacy
    tail = text.split(head, 1)[1] if head in text else ""
    tail = tail.split("\n", 1)[0].strip().rstrip("]").strip()
    if tail:
        gist = tail if len(tail) <= 220 else tail[:217].rsplit(" ", 1)[0] + "…"
    note = f"[\U0001F4F7 Photo — {gist}]" if gist else "[\U0001F4F7 Photo]"
    return f"{caption}\n\n{note}" if caption else note


_JSON_RECOVERY_NUDGE = (
    "(That came out as raw JSON instead of doing anything. If you meant to use a tool, call it "
    "properly as a tool. Otherwise just answer me in plain words — never paste JSON at me.)"
)
_LEAKED_TOOL_JSON = re.compile(
    r'^\s*[\[{].*"(name|tool_name|function|arguments|parameters)"\s*:', re.IGNORECASE | re.DOTALL
)


def _too_similar(current: str, previous: str) -> bool:
    """Is this reply essentially a repeat of the last one? Used to stop Sali saying the same thing
    over and over when it's stuck (e.g. a tool keeps failing and it keeps narrating 'let me retry')."""
    if not current or not previous:
        return False
    a, b = " ".join(current.lower().split()), " ".join(previous.lower().split())
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() > 0.85


def _looks_like_leaked_tool_call(text: str) -> bool:
    """A response whose content is really a tool-call JSON blob the parser didn't catch. We treat
    it as a malfunction to recover from, not as an answer to show."""
    stripped = text.strip()
    return stripped.startswith(("{", "[")) and bool(_LEAKED_TOOL_JSON.match(stripped))


# A weaker model sometimes writes a tool call as PSEUDOCODE — `plan_task("Build X", "steps…")` — in
# the content instead of emitting a structured call the provider can parse. The tool never runs, and
# the raw call text would be shown to Almir as if it were an answer (observed live: a whole todo-API
# request came back as the literal text `plan_task("Build Todo API", …)` and no task was created).
# We detect it and recover exactly like a JSON leak. Gated on a REAL tool name so ordinary prose that
# happens to contain "something(" is never caught.
_LEAKED_FUNC_CALL = re.compile(r"^\s*([a-zA-Z_][a-zA-Z0-9_]{2,40})\s*\(")


def _looks_like_leaked_call(text: str, tool_names: frozenset[str]) -> bool:
    """True when the reply is really a tool call the parser missed — a JSON blob OR pseudocode
    `tool_name(...)` whose leading identifier is an actual tool. Not an answer: the tool never ran."""
    if _looks_like_leaked_tool_call(text):
        return True
    m = _LEAKED_FUNC_CALL.match(text.strip())
    return bool(m and m.group(1) in tool_names)


_LEAKED_CALL_NUDGE = (
    "(You wrote a tool call as text instead of actually calling it, so nothing happened — no task was "
    "created, no file written. If you meant to use that tool, CALL it properly as a tool; do not type "
    "its name and arguments as prose. Otherwise just answer me in plain words.)"
)


class _TokenGate:
    """Streams Sali's words live, but holds back a reply that is really a leaked tool-call JSON
    blob so the raw JSON is never shown (the loop then has the model redo it cleanly). It keys on
    the same signal as `_looks_like_leaked_tool_call`: real prose never opens with '{' or '[', a
    leaked blob always does. So the first non-whitespace character decides — prose is released and
    then flows live; a blob is withheld for the whole turn. Structural, not a prompt rule."""

    __slots__ = ("_buf", "_streaming")

    def __init__(self) -> None:
        self._buf = ""
        self._streaming: bool | None = None  # None = undecided, True = prose, False = withholding

    def feed(self, chunk: str) -> str:
        """Return the text safe to show now — '' while still undecided or withholding a blob."""
        if self._streaming is True:
            return chunk
        self._buf += chunk
        if self._streaming is False:
            return ""
        head = self._buf.lstrip()
        if not head:
            return ""  # only whitespace so far — can't tell yet
        if head[0] in "{[":
            self._streaming = False  # opens like a tool-call blob — hold it back for recovery
            return ""
        self._streaming = True  # it's prose — release what's buffered and stream live from here
        out, self._buf = self._buf, ""
        return out


# Stall detection is structural, not a phrase list, and has NO length cutoff: a stall can hide at
# the end of a long explanation ("…let me now write all the files"), so every tool-less reply is
# put to the model, which judges its OWN reply — did it finish, or only announce/half-do it? This
# is what the model is for (judgement, not parsing), and it can't drift out of date like a verb list.
_STALL_JUDGE_SYSTEM = (
    "Almir asked you to do something and you just replied. Judge your OWN reply against the FULL "
    "task: did you actually finish everything he asked and give him the result — or did you only "
    "announce it, do PART of it, or say you'll 'continue' / 'keep building' / 'work on it' WITHOUT "
    "actually finishing in this reply? You have no background process, so if there's more to do, it "
    "is NOT done. A genuine question back to Almir, or a fully-finished task, counts as DONE. Reply "
    "with exactly one word: DONE or STALLED.\n\n"
    "You are also shown what you ACTUALLY did this turn — the tools that ran and whether each worked. "
    "That record is the evidence; your reply is only a claim about it. If the reply says you created, "
    "installed, sent, fixed or finished something and the record does not show it happening, that is "
    "STALLED, however confidently it was written."
)


def _render_tool_record(record: list[tuple[str, bool]]) -> str:
    """The turn's tool history in one line, for the stall judge. Failures are named explicitly: a tool
    that ran and failed is not evidence that the work happened, and collapsing the two is how "I fixed
    it" survives a turn where the fix errored."""
    if not record:
        return "nothing — you called no tools"
    return ", ".join(f"{name} ({'worked' if ok else 'FAILED'})" for name, ok in record)


# GOAL DETECTION - stricter than the memory-capture gate, because a goal is a persistent,
# multi-step objective that will feed the agenda and shape future planning. A task IS the way you
# ask Sali to do something; a goal is the OBJECTIVE that spans many tasks. Only explicit signals
# (a "goal:" prefix, "our/my/the goal is", "let\'s build/deploy/ship <X>", "add this as a goal") -
# because the false-positive direction is destructive here (the goal table fills with noise and
# the agenda stops meaning anything), and the false-negative direction is recoverable (Almir can
# rephrase or say "make this a goal").
_GOAL_RE = re.compile(
    r"\b(goal:|"
    r"(our|my|the) goal is|"
    r"i want us to (build|deploy|ship|launch|create|migrate|set\s?up|automate|integrate)|"
    r"we should (build|deploy|ship|launch|create|migrate)|"
    r"let'?s (build|deploy|ship|launch|create|migrate|set\s?up|automate|integrate)|"
    r"add (this|that) (as|to) (a )?(goal|goals|agenda)|"
    r"make (this|that) (a )?goal|"
    r"long[-\s]?term (i|we) want|"
    r"the objective is|"
    r"we'?re (building|deploying|launching|migrating) (?:a |the |our )?[a-z0-9])"
    ,
    re.IGNORECASE,
)


def _goal_signal(text: str) -> bool:
    """Is Almir declaring a persistent objective (not just asking Sali to do a thing)?

    Conservative gate - see the _GOAL_RE docstring. Only explicit persistence signals qualify.
    A one-off request like "make me a script" is a task, not a goal, and lands in the task
    system through the existing planning path."""
    return bool(text and len(text) < 4000 and _GOAL_RE.search(text))


_GOAL_PROMPT = (
    "Almir just said:\n\n{msg}\n\n"
    "If this states a durable OBJECTIVE - something spanning multiple tasks or a long-term aim,"
    " not a one-off request - reply on ONE line, exactly:\n\n"
    "GOAL: <one clear sentence naming the objective, written about it from Sali\'s point of view>\n\n"
    "Otherwise reply with exactly: NONE\n"
    "No other output. No explanation. No JSON."
)



_KW_STOP = frozenset({
    "the","a","an","and","or","but","of","to","for","with","in","on","at","by","from","as",
    "is","are","was","were","be","been","being","this","that","these","those","it","its","our",
    "we","us","you","your","my","me","i","he","she","they","them","up","let","lets","let\'s",
    "want","need","should","would","could","will","shall","can","may","might","must",
    "goal","objective","target","aim","build","deploy","ship","launch","create","make",
})


def _keywords(text: str) -> set[str]:
    """Content words for coarse similarity. Not a real NLP tokenizer - just enough to compare
    two short objective sentences and decide whether they name the same thing. The stopword
    list deliberately includes goal/objective/build/deploy so \"deploy Salieno\" and \"let\'s
    deploy Salieno\" collapse to just {\"salieno\"} + tokens - the target signal is the noun."""
    words = [w.strip(".,;:!?()[]{}\"'`").lower() for w in (text or "").split()]
    return {w for w in words if w and len(w) >= 3 and w not in _KW_STOP}


# DURABLE FACTS stated NATURALLY (no "remember" keyword): introducing a person ("this is my wife Faith",
# "Faith is my wife") or a possessive fact ("my project is TanzHost"). The gate missed EXACTLY these — the
# most personalization-critical statements — so a companion never learned Almir's people or projects
# (§ Phase 5). The KEEP/SKIP model filter still decides what actually persists, so widening the gate only
# means those turns get considered; noise is dropped downstream.
_REL_TERMS: dict[str, str] = {
    "wife": "married_to", "husband": "married_to", "spouse": "married_to", "partner": "partner_of",
    "fiance": "engaged_to", "fiancee": "engaged_to", "girlfriend": "partner_of", "boyfriend": "partner_of",
    "son": "parent_of", "daughter": "parent_of", "kid": "parent_of", "child": "parent_of",
    "mother": "child_of", "mom": "child_of", "father": "child_of", "dad": "child_of", "parent": "child_of",
    "brother": "sibling_of", "sister": "sibling_of", "sibling": "sibling_of",
    "friend": "friend_of", "boss": "reports_to", "colleague": "colleague_of", "coworker": "colleague_of",
}
_REL_ALT = "|".join(_REL_TERMS)
_POSSESSIVE_NOUNS = (r"business|company|startup|project|app|product|website|domain|team|dog|cat|pet"
                     r"|car|house|home|apartment|office")
_PROPER_NAME = r"[A-Z][a-zA-Z'’\-]+(?:\s+[A-Z][a-zA-Z'’\-]+){0,2}"
_DURABLE_FACT_RE = re.compile(
    rf"\b(?:this\s+is\s+my|(?:that|these)\s+(?:is|are)\s+my)\s+(?:{_REL_ALT})s?\b"
    rf"|\bmy\s+(?:{_REL_ALT})\b\s*(?:is|,|:|—|-|\bnamed\b|\bcalled\b)"
    rf"|\bmy\s+(?:{_REL_ALT})\s+{_PROPER_NAME}"   # bare-name intro: "my brother Tom"
    rf"|\b[A-Za-z][\w'’\-]+\s+is\s+my\s+(?:{_REL_ALT})\b"
    rf"|\bmy\s+(?:\w+\s+){{0,2}}(?:{_POSSESSIVE_NOUNS})\s+is\b",   # allow "my main/current project is"
    re.IGNORECASE)
# A sentence-initial pronoun/demonstrative/entity is capitalised too, so _PROPER_NAME will happily pull
# "This"/"He"/"God"/"Nobody" in as a "name". Never mint a person node from one of these.
_NOT_A_NAME = frozenset({"This", "That", "These", "Those", "He", "She", "They", "It", "We", "You", "I",
                         "There", "Here", "God", "Nobody", "Everybody", "Everyone", "Someone", "Anyone",
                         "Somebody", "Anybody", "No", "Yes", "Who", "What", "My", "The", "A", "An"})
# Extract (person_name, rel_type) for a graph edge. NO IGNORECASE: a real name is capitalised, the
# relation words are lowercase (Almir's phrasing) — so we don't pull a lowercase word in as a "name".
# The demonstrative/second-person branch is tried FIRST so "This is my wife Faith" resolves to Faith,
# not to name1="This" (the name1 branch would otherwise short-circuit on the capitalised "This").
_REL_EXTRACT_RE = re.compile(
    rf"(?:[Tt]his|[Tt]hat|[Tt]hese)\s+(?:is|are)\s+my\s+(?P<rel2>{_REL_ALT})\b[,;:\s]+"
    rf"(?:(?:her|his|their)\s+name\s+is\s+)?(?P<name2>{_PROPER_NAME})"
    rf"|my\s+(?P<rel4>{_REL_ALT}),\s*(?P<name4>{_PROPER_NAME})"   # comma-appositive: "my wife, Faith,"
    rf"|(?P<name1>{_PROPER_NAME})\s+is\s+my\s+(?P<rel1>{_REL_ALT})\b"
    rf"|my\s+(?P<rel3>{_REL_ALT})\s+(?:is\s+(?:named\s+|called\s+)?)?(?P<name3>{_PROPER_NAME})")


def _extract_relationship(text: str) -> tuple[str, str] | None:
    """(person_name, rel_type) when the text states one of Almir's relationships, else None."""
    m = _REL_EXTRACT_RE.search(text or "")
    if not m:
        return None
    g = m.groupdict()
    name = (g.get("name1") or g.get("name2") or g.get("name3") or g.get("name4") or "").strip()
    rel = (g.get("rel1") or g.get("rel2") or g.get("rel3") or g.get("rel4") or "").strip().lower()
    if not name or name in _NOT_A_NAME or rel not in _REL_TERMS:
        return None
    return name, _REL_TERMS[rel]


def _durable_signal(text: str, reply: str = "") -> bool:
    """Should this turn leave something durable behind?

    Two independent triggers, because either one alone leaks. Almir's phrasing is matched for the cases
    where he clearly means a fact to persist — but any pattern over natural language has holes, and the
    hole is expensive: he says something, Sali answers "noted", nothing is written, and he finds out days
    later. So Sali's OWN reply is the second trigger. If it told him it would remember, that is a promise
    the runtime keeps on its behalf.
    """
    from sali.context.engine import _CLAIMED_MEMORY_RE, _CORRECT_RE, _STORE_RE
    return bool(
        _STORE_RE.search(text or "")
        or _CORRECT_RE.search(text or "")
        or _DURABLE_FACT_RE.search(text or "")   # relationship intros + possessive facts (§ Phase 5)
        or _CLAIMED_MEMORY_RE.search(reply or "")
    )


_CAPTURE_PROMPT = (
    "Almir said this to Sali:\n\n{msg}\n\nSali replied:\n\n{reply}\n\n"
    "If he stated something durable that should outlive this conversation — a preference, a "
    "standing rule, a correction, a fact about him or his setup — reply on ONE line, exactly like "
    "this:\n\n"
    "KEEP: <the fact in one plain sentence, written to him as \"you\">\n\n"
    "Otherwise reply with exactly: SKIP\n"
    "No other output. No explanation. No JSON."
)


# Kind inference for a captured statement. Kept as vocabulary-only heuristics because these
# categories are also what _LAYER_OF maps to: preference/identity/procedure/environment/fact go to
# distinct memory layers and get retrieved differently. A wrong kind lands the memory in the wrong
# layer, which is worse than a permissive default of "fact" (semantic).
# Topic extraction for the correction/supersession anchor. `_MemorySink.remember` uses `about` to
# build a `claim_key = "remember:<topic>"`, which is what lets a later fact on the same topic
# supersede an earlier one instead of piling up ("you use Neovim" replacing "you use VS Code" rather
# than living next to it). The JSON prompt used to ask the model for it; the two-shape KEEP/SKIP
# parser dropped it. Preferences have a layer-specific fallback in the sink, so those still work -
# but a stated fact ("i work from Dar", "my name is Almir") lost the anchor and stopped superseding.
#
# Cheap noun-phrase heuristics: `your <noun>` / `you <verb> ... <object>` / possessives / `call me`.
# A miss leaves `about=None` and the sink handles it (falling back on layer defaults or nothing);
# a false hit picks the wrong topic but a later restatement with the same topic still supersedes
# correctly. Conservative wins: prefer no anchor to a wrong one.

# Order matters: multi-word possessive phrases must try FIRST, so "your hosting business" wins
# before the plain noun list can pick "host" out of "hosting" and return the wrong topic. 
# also removed from the noun list for the same reason - it was ambiguous with hostname vs hosting.
# Multi-word modifiers between "your" and the noun ("your primary text editor") handled by an
# optional [modifier]* slot rather than a fixed single-token modifier.
_ABOUT_RE = re.compile(
    r"\byour\s+(?P<poss>hosting\s+business|main\s+project|day\s+job|primary\s+language)\b"
    r"|\byour\s+(?:\w+\s+){0,3}(?P<n>editor|browser|shell|terminal|ide|language|framework|"
    r"database|machine|pc|computer|home|workspace|project|company|business|"
    r"name|role|title|team|timezone|city|country|address|email|phone|"
    r"schedule|preference|style|approach|methodology|convention)"
    r"|\bcall\s+(?P<call>you|me|almir)\s+"
    r"|\byou\s+(work|live|are\s+based)\s+(from|in|at)\s+",
    re.IGNORECASE,
)


def _infer_about(sentence: str) -> str | None:
    """The topic this sentence is about, for the supersession key. None when nothing recognisable
    surfaces - the sink then falls back on the layer default (preference gets a synthesised key;
    fact stays anchor-free and accumulates rather than superseding)."""
    m = _ABOUT_RE.search(sentence or "")
    if not m:
        return None
    for group in ("n", "poss"):
        val = m.groupdict().get(group)
        if val:
            return val.lower().split()[-1]
    # "call me/you/almir" - the topic is naming
    if m.groupdict().get("call"):
        return "name"
    # "you work from / live in ..." - the topic is location
    return "location"


def _infer_kind(sentence: str) -> str:
    lowered = " " + (sentence or "").lower() + " "   # so leading-word patterns work at sentence start
    if any(w in lowered for w in
           (" wife", " husband", " spouse", " partner", " fianc", " girlfriend", " boyfriend",
            " son ", " daughter", " kid", " child", " mother", " mom ", " father", " dad ",
            " brother", " sister", " sibling", " friend", " colleague")):
        return "relationship"
    if any(w in lowered for w in
           (" prefer", " like ", " likes ", " dislike", " want ", " favorite", " favourite",
            " always ", " never ", "call me")):
        return "preference"
    if any(w in lowered for w in (" i am ", " you are ", " your name", "name is ", " i'm ")):
        return "identity"
    if any(w in lowered for w in
           (" this machine", " this pc", " this computer", " /home/", " /usr/", " /etc/",
            " installed", " running", " version", " service", " path")):
        return "environment"
    if any(w in lowered for w in (" step ", " first ", " then ", " to do this", " procedure")):
        return "procedure"
    return "fact"


class _MemorySink:
    """Bridges the remember tool to the memory system: write a durable fact, then embed it so it's
    immediately recallable. Injected into ToolContext so the tools layer needn't import memory."""

    def __init__(self, service: Any) -> None:  # MemoryService
        self._service = service

    # Which layer a remembered thing belongs in. `MemoryLayer` has had seven values since the schema
    # was written, and until now this sink hardcoded SEMANTIC for every one of them — so PREFERENCE,
    # PROCEDURAL and IDENTITY were unreachable by any code path in the system. Measured on a clean
    # database after a wipe: of the layers actually written, only `system_env` (the perception
    # faculty's machine inventory) and `episodic` (turn summaries) ever appeared. Telling Sali "my
    # preferred editor is Helix" could not produce a preference memory, because nothing could write one.
    _LAYER_OF = {
        "fact": MemoryLayer.SEMANTIC,
        "preference": MemoryLayer.PREFERENCE,
        "procedure": MemoryLayer.PROCEDURAL,
        "identity": MemoryLayer.IDENTITY,
        "environment": MemoryLayer.SYSTEM_ENV,
    }

    async def remember(
        self, content: str, *, source: MemorySource, note: str | None = None,
        importance: float = 0.6, needs_grounding: bool = False, about: str | None = None,
        kind: str | None = None,
    ) -> None:
        # An `about` topic makes this a functional claim: restating a fact about the same topic
        # supersedes the old value (evidence-priority) instead of piling up a contradiction.
        claim_key = f"remember:{about.strip().lower()}" if about and about.strip() else None
        layer = self._LAYER_OF.get((kind or "").strip().lower(), MemoryLayer.SEMANTIC)
        # Belt under the caller's judgement: whatever route this arrived by, a claim about this machine
        # is unverified until something actually looks.
        needs_grounding = needs_grounding or looks_checkable(content)
        # A preference is single-valued by nature — "my editor is X" replaces "my editor is Y", it does
        # not accumulate. So a preference without an explicit topic still gets a claim key, otherwise
        # the correction path can never fire on exactly the class of memory that changes most often.
        if claim_key is None and layer is MemoryLayer.PREFERENCE and about is None:
            claim_key = f"preference:{content.strip().lower()[:60]}"
        await self._service.remember(
            layer=layer, content=content, source=source,
            importance=importance, note=note, needs_grounding=needs_grounding,
            functional=bool(claim_key), claim_key=claim_key,
        )
        await self._service.embed_pending()

    async def forget(self, query: str, *, reason: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._service.forget_matching(query, reason=reason)
        return result

    async def verify(self, query: str, *, verified: bool, note: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = await self._service.verify_matching(query, verified=verified, note=note)
        return result


class _SelfSink:
    """Exposes Sali's runtime self-model to the self_state tool (§6/§7/§41/§71). Read-only."""

    def __init__(self, store: SelfStateStore) -> None:
        self._store = store

    async def report(self) -> dict[str, Any]:
        return await self._store.assemble()


class _HealthSink:
    """Exposes Sali's live subsystem/internet health to the system_health tool (§51/§52/§53)."""

    def __init__(self, service: HealthService) -> None:
        self._service = service

    async def report(self) -> dict[str, Any]:
        h = await self._service.check(max_age_s=45.0)
        return {"subsystems": h.subsystems, "degraded": h.degraded, "online": h.internet,
                "summary": h.render(), "detail": h.detail}


class _ToolCatalogSink:
    """Lets Sali query its OWN toolset (§53/§21): which installed tools serve a task (ranked by learned
    reliability + safety) and a tool's alternatives — over the inventory + capability graph. Read-only."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def suggest(self, task: str) -> list[dict[str, Any]]:
        from sali.twin import selection

        async with self._pool.acquire() as conn:
            ranked = await selection.suggest(conn, task)
        return [{"tool": s.name, "capability": s.capability, "score": s.score,
                 "reliability": s.reliability, "used": s.used, "authority": s.authority}
                for s in ranked]

    async def alternatives(self, tool: str) -> list[str]:
        from sali.twin import selection

        async with self._pool.acquire() as conn:
            return await selection.alternatives(conn, tool)


class _RecallSink:
    """Active memory recall for Sali's memory tools (§34,§55,§56): hybrid search, graph traversal, and
    history — every result carries provenance + confidence + a staleness flag so the model reasons
    over evidence and never has to invent a memory (§47). Read-only, over the retrieval + graph engines."""

    def __init__(self, memory: Any, graph: Any) -> None:  # MemoryService, GraphService
        self._memory = memory
        self._graph = graph

    async def search(self, query: str, *, layer: str | None = None, k: int = 6) -> list[dict[str, Any]]:
        # A layer-scoped read is a REAL SQL predicate now, so a relevant procedure/incident outside
        # the generic top-k is no longer invisible (fixes the top-24 blind spot; §4/§6).
        hits = await self._memory.retrieve(query, k=k, layer=layer)
        out: list[dict[str, Any]] = []
        for h in hits[:k]:
            m = h.memory
            # The epistemic kind — did Sali OBSERVE this, INFER it, or merely BELIEVE it? — so the model
            # weights each recalled item by how it's actually known, not by how fluent it sounds (§3/§12).
            ktype = classify_knowledge(m.source, m.layer, needs_grounding=m.needs_grounding,
                                       confidence=h.effective_confidence)
            entry: dict[str, Any] = {
                "content": m.content, "layer": m.layer.value, "source": m.source.value,
                "knowledge_type": ktype.value,
                "epistemic_status": epistemic_status(ktype, h.effective_confidence),
                "confidence": round(h.effective_confidence, 2), "stale": h.stale,
                "recorded": m.valid_from.date().isoformat() if m.valid_from else None,
            }
            if m.structured:  # a structured incident/experience/procedure — give Sali the whole shape
                entry["detail"] = m.structured
            out.append(entry)
        return out

    async def related(self, entity: str, *, hops: int = 1) -> dict[str, Any]:
        # Resolve EXPLAINABLY (§5/§6): which entity was chosen, how, and the others the name matched —
        # so two queries can never silently disagree about which 'Sali' they meant.
        res = await self._graph.resolve(entity)
        if res.resolved is None:
            return {"entity": entity, "found": False, "relations": [], "candidates": []}
        node = res.resolved
        relations: list[dict[str, Any]] = []
        if hops <= 1:
            for hop in await self._graph.neighbors(node.id):
                relations.append({"relation": hop["rel_type"], "target": hop["node"].name,
                                  "type": hop["node"].node_type, "confidence": round(hop["confidence"], 2)})
        else:
            for r in await self._graph.traverse(node.id, max_depth=min(hops, 4)):
                if r["depth"] > 0:
                    relations.append({"target": r["name"], "type": r["node_type"], "hops": r["depth"]})
        others = [c for c in res.candidates if c["canonical_key"] != node.canonical_key][:5]
        return {
            "entity": entity, "found": True,
            "resolved": {"name": node.name, "canonical_key": node.canonical_key,
                         "node_type": node.node_type, "matched_by": res.matched_by},
            "ambiguous": res.ambiguous, "other_candidates": others,
            "scope": "current graph (temporal-filtered, valid_until IS NULL)",
            "relations": relations[:24],
        }

    async def entity(self, name: str) -> dict[str, Any]:
        nodes = await self._graph.find_by_name(name, limit=3)
        if not nodes:
            return {"name": name, "found": False}
        node = nodes[0]
        neighbours = await self._graph.neighbors(node.id)
        return {"name": node.name, "found": True, "type": node.node_type,
                "confidence": round(node.confidence, 2), "props": redact_obj(dict(node.props)),
                "relations": [{"relation": h["rel_type"], "target": h["node"].name} for h in neighbours[:24]]}

    async def history(self, entity: str, relation: str) -> dict[str, Any]:
        nodes = await self._graph.find_by_name(entity, limit=1)
        if not nodes:
            return {"entity": entity, "found": False, "timeline": []}
        node = nodes[0]
        rel = relation.strip().lower().replace(" ", "_")
        edges = await self._graph.history(node.id, rel)
        names = await self._graph.names_for([e.dst_id for e in edges])
        timeline = [{
            "value": names.get(e.dst_id, str(e.dst_id)),
            "from": e.valid_from.date().isoformat() if e.valid_from else None,
            "until": e.valid_until.date().isoformat() if e.valid_until else None,
            "current": e.valid_until is None,
        } for e in edges]
        return {"entity": node.name, "relation": rel, "found": bool(timeline), "timeline": timeline}


class _VisionSink:
    """Bridges the see_screen tool to the LOCAL vision model. The screenshot bytes go only to the
    provider (local Ollama) and are never stored — the tools layer never imports the provider."""

    def __init__(self, provider: Any) -> None:  # ModelProvider
        self._provider = provider

    async def look(self, prompt: str, image: bytes) -> str:
        return str(await self._provider.describe_image(prompt, image))


class _ResearchSink:
    """Just-in-time web research bound to the current task (Prompt 5 §14-20). Searches the web, distills
    a bounded extractive summary, and persists it as durable, task-linked EVIDENCE (never temporary
    context, never automatic permanent truth). Records evidence-aware learning candidates, and promotes
    only VERIFIED ones into durable memory. Injected into ToolContext so the tools layer stays clean."""

    def __init__(self, *, store: Any, tasks: TaskStore, run_id: UUID | None, pool: Any) -> None:
        self._store = store
        self._tasks = tasks
        self._run_id = run_id
        self._pool = pool

    async def _current_task_id(self) -> UUID | None:
        with contextlib.suppress(Exception):
            t = await self._tasks.current()
            return t.id if t is not None else None
        return None

    async def research(self, query: str, *, step_seq: int | None = None) -> dict[str, Any]:
        from sali.tools.builtins.web import WebSearch
        from sali.tools.context import local_context

        task_id = await self._current_task_id()
        with contextlib.suppress(Exception):
            if self._store._publisher is not None:
                await self._store._publisher.emit(
                    event_type="research.started", task_id=task_id, run_id=self._run_id,
                    subject_type="task", subject_id=task_id, origin="research",
                    data={"query": query[:200]})
        results: list[dict[str, Any]] = []
        with contextlib.suppress(Exception):
            res = await WebSearch().run({"query": query}, local_context())
            results = list(res.output.get("results", [])) if res.ok else []
        if not results:
            # research is durable even when it FAILS — the task keeps its progress, never restarts (§21)
            await self._store.record_research(
                task_id=task_id, run_id=self._run_id, step_seq=step_seq, query=query, source=None,
                summary="(no results — research blocked or offline)", confidence=0.0,
                content_hash=None, status="failed")
            return {"ok": False, "reason": "no_results", "query": query}
        top = results[:3]
        summary = " | ".join((r.get("snippet") or "").strip() for r in top if r.get("snippet"))[:800]
        source = top[0].get("url")
        h = hashlib.sha256((summary or query).encode("utf-8")).hexdigest()[:16]
        rid = await self._store.record_research(
            task_id=task_id, run_id=self._run_id, step_seq=step_seq, query=query, source=source,
            summary=summary or "(sources found, no snippet)", confidence=0.6, content_hash=h)
        return {"ok": True, "research_id": str(rid), "summary": summary, "source": source,
                "sources": [r.get("url") for r in top]}

    async def record_lesson(
        self, lesson: str, *, source: str | None = None, research_id: str | None = None,
    ) -> dict[str, Any]:
        task_id = await self._current_task_id()
        rid = None
        with contextlib.suppress(Exception):
            rid = UUID(research_id) if research_id else None
        cid = await self._store.record_candidate(
            task_id=task_id, run_id=self._run_id, lesson=lesson, source=source, research_id=rid)
        if rid is not None:
            with contextlib.suppress(Exception):
                await self._store.mark_used(rid, task_id, self._run_id)
        return {"ok": True, "candidate_id": str(cid), "verification_state": "unverified"}

    async def promote_verified(self) -> int:
        """Promote every VERIFIED, not-yet-promoted candidate into durable memory (§19). A candidate is
        verified only by real completion evidence (the reviewer PASS), so a failed experiment is never
        promoted (§20). Idempotent — the promoted flag prevents double-writes."""
        from sali.core.enums import MemoryLayer, MemorySource
        from sali.memory.writer import remember as mem_remember

        promoted = 0
        for c in await self._store.promotable_candidates():
            with contextlib.suppress(Exception):
                async with self._pool.acquire() as conn:
                    await mem_remember(
                        conn, layer=MemoryLayer.SEMANTIC, content=c["lesson"],
                        source=MemorySource.EXTERNAL_SOURCE, needs_grounding=True, importance=0.5,
                        note=f"verified learning candidate: {c.get('source') or ''}",
                        structured={"kind": "learned_lesson", "candidate_id": str(c["id"])})
                await self._store.mark_promoted(c["id"], task_id=c.get("task_id"))
                promoted += 1
        return promoted


# `_DelegateSink` removed (brain-audit turn 8). Sali is ONE executive agent — no subagent runtime,
# no refuse-scaffold. `delegate()` never ran anything in production (the sink held a DelegationStore
# it never touched) and only existed to satisfy a ToolContext channel that has also been removed.


class _ClarifySink:
    """Let Sali pause the primary task and ask the user a clarifying question (§44). Durable — the task
    becomes 'waiting_for_user' and resumes on the user's next message; never a failure, nothing lost."""

    def __init__(self, *, store: Any, tasks: TaskStore, run_id: UUID | None) -> None:
        self._store = store
        self._tasks = tasks
        self._run_id = run_id

    async def ask(self, question: str) -> dict[str, Any]:
        task = None
        with contextlib.suppress(Exception):
            task = await self._tasks.current()
        if task is None:
            return {"ok": False, "reason": "no active task to pause"}
        qid = await self._store.ask(task.id, question, run_id=self._run_id)
        return {"ok": True, "question_id": str(qid), "status": "waiting_for_user"}


@dataclass(slots=True)
class AgentResult:
    run_id: UUID
    text: str
    iterations: int
    tool_calls: int


@dataclass(slots=True)
class LoopEvent:
    """A streamed moment of a turn, for live rendering (terminal or WebSocket)."""

    kind: str  # 'run' | 'status' | 'token' | 'thinking' | 'tool' | 'retrieval' | 'final' | 'error'
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class AgentLoop:
    def __init__(
        self,
        *,
        pool: Any,
        provider: ModelProvider,
        retrieval: RetrievalService,
        context: ContextEngine,
        registry: ToolRegistry,
        policy: PolicyEngine,
        confirmer: Confirmer,
        settings: Settings,
        clock: Clock | None = None,
        learning: Any = None,
    ) -> None:
        self.pool = pool
        self.provider = provider
        self.retrieval = retrieval
        self.context = context
        self.registry = registry
        self.policy = policy
        self.confirmer = confirmer
        self.settings = settings
        self.learning = learning  # LearningService | None — drives §17-19 consolidation off-thread
        self.clock = clock or SystemClock()
        # THE ONE PLACE "NOW" BECOMES MEANING. Built on the same injected clock, so a frozen clock in a
        # test freezes every temporal answer with it — and so there is exactly one notion of the
        # current instant rather than one per component.
        self.temporal = TemporalService(
            self.clock, owner_timezone=settings.temporal.owner_timezone)
        self.log = get_logger("sali.loop")
        self._publisher: Any = None  # EventPublisher — set by runtime after construction
        self._reviewer: Any = None  # TaskReviewer — the completion gate; set by runtime (Prompt 4)
        self._memory_sink = _MemorySink(retrieval.memory)  # lets the remember tool save durably
        self._graph = GraphService(pool)  # lets the relate tool write conversational edges
        self._recall = _RecallSink(retrieval.memory, self._graph)  # lets memory tools actively query (§34)
        self._tasks = TaskStore(pool)  # persistent multi-step tasks (§24), resumed across restarts
        from sali.skills.store import SkillStore
        from sali.tasks.ledger import DecisionStore, PhaseStore
        from sali.tasks.research import ResearchStore
        self._skills = SkillStore(pool)  # per-task skill snapshots (Prompt 5); publisher set by runtime
        self._skills_root = settings.permissions.skills_root
        self._research_store = ResearchStore(pool)  # task-bound research + learning candidates (Prompt 5)
        self._decisions = DecisionStore(pool)  # decision ledger (Prompt 6 §30); publisher set by runtime
        self._phases = PhaseStore(pool)        # task phases (Prompt 6 §31); publisher set by runtime
        self._repeat_warned: set[Any] = set()  # (tool,error) already flagged as a repeated failure (§45)
        # KNOWN-BAD CALLS, PER SESSION, ACROSS TURNS.
        #
        # The circuit breaker that refuses a call which has already failed the same way lived as a local
        # inside astream(), so it was rebuilt empty on every user turn: a command that failed three
        # times a moment ago was re-issued with full confidence the instant Almir said anything else.
        # That is the §21 failure — repeating a mistake he had already made — and it was invisible
        # because within a single turn the guard looked like it worked.
        #
        # Session-scoped, time-limited and cleared by success, because a call that fails is not
        # permanently doomed: Almir installs the missing package, or the service comes up, and Sali must
        # be able to try again rather than be stuck refusing forever.
        self._failed_sigs: dict[Any, dict[str, tuple[int, str, float]]] = {}
        # Compact tasks in flight, one per session. Cancelled the moment the same session speaks again
        # (a rapid follow-up cannot afford to wait for a fold), retained across turns of a QUIET session
        # so the fold actually finishes.
        self._compact_tasks: dict[Any, Any] = {}
        # DelegationStore removed (brain-audit turn 8): Sali is a single executive agent. The store
        # + its 0-or-1 partial-unique DB index protected nothing since the sink never wrote to it.
        from sali.tasks.coordination import QuestionStore
        self._questions = QuestionStore(pool)      # user-clarification questions (§44)
        self._task_authority = TaskAuthority(self._tasks)  # deterministic active-task enforcement
        self._schedules = ScheduleStore(  # recurring work (§44)
            pool, self.clock, owner_timezone=settings.temporal.owner_timezone)
        self._documents = IngestService(pool, provider)  # document ingestion → memory (§44)
        self._remote = build_remote_runner(settings.ssh, SecretStore())  # ssh; vault-backed passwords
        self._comms = CommsService(settings.comms, SecretStore())  # email + calendar (§44)
        self._browser = build_browser(settings.browser)  # Sali's own Firefox (§44); launched lazily
        self._vision = _VisionSink(provider)  # look at the screen locally (sali3 §33-35)
        self._perception = build_perception(settings)  # focused app/window + a11y tree (sali3 §2,8,9)
        self._catalog = _ToolCatalogSink(pool)  # lets Sali query its own toolset (§53/§21)
        self._self_state = SelfStateStore(pool)  # persistent runtime self-model (§6/§7/§41)
        self._self_sink = _SelfSink(self._self_state)
        self._world = WorldStateBuilder(pool, self._perception)  # live "what's happening now" (§73)
        self._health = _HealthSink(HealthService(pool, provider, perception=self._perception))  # §51-53
        self._syscrit: tuple[frozenset[str], float] | None = None  # (system-critical binaries, loaded_at)
        self._consolidating: asyncio.Task[Any] | None = None
        self._last_consolidate: Any = None

    async def aclose(self) -> None:
        """Release loop-lifetime resources (Sali's live browser). Best-effort; safe to call twice."""
        with contextlib.suppress(Exception):
            await self._browser.aclose()

    async def run(
        self, user_input: str, session_id: UUID | None = None, *, as_subagent: bool = False,
        internal: bool = False,
    ) -> AgentResult:
        """Run one turn to completion (non-streaming) by consuming the event stream."""
        final: LoopEvent | None = None
        async for event in self.astream(user_input, session_id, as_subagent=as_subagent,
                                        internal=internal):
            if event.kind == "final":
                final = event
        if final is None:  # pragma: no cover - astream always yields final or raises
            raise RuntimeError("agent loop produced no final event")
        return AgentResult(
            run_id=UUID(final.data["run_id"]), text=final.text,
            iterations=int(final.data["iterations"]), tool_calls=int(final.data["tool_calls"]),
        )

    async def _voice_stream(
        self, messages: list[ChatMessage], journal: RunJournal
    ) -> AsyncIterator[LoopEvent]:
        """Stream a plain-voice wrap-up with the SAME provider-retry the main turn uses, so a
        transient model error during the wrap-up doesn't turn the reply into a crash. Yields 'token'
        events live and, last, a '_final' event carrying the full text for the caller to read off."""
        attempt = 0
        while True:
            acc = ""
            try:
                async for chunk in self.provider.chat_stream(
                        messages, options=_VOICE, preset=CREATIVE):
                    if chunk.content:
                        acc += chunk.content
                        # Content is NOT streamed live here — the final answer is typed out in one
                        # clean paced pass at the end of the turn (see the final-answer pace-emit).
                        # Streaming it live from here mixed reasoning/wrap-up prose into the bubble.
                yield LoopEvent("_final", acc)
                return
            except ProviderError as exc:
                attempt += 1
                if attempt > _MAX_PROVIDER_RETRIES:
                    yield LoopEvent("_final", acc)  # give back whatever streamed; a fallback covers empty
                    return
                await journal.event("provider_retry", {"attempt": attempt, "error": str(exc)[:200]})
                yield LoopEvent("status", "let me try that again")

    async def _summarise_for_chat(self, full_text: str) -> str | None:
        """A short chat summary of a long research answer — the highlights only, so the chat stays
        readable while the full detail lives in the downloadable report (Almir §). Bounded, low-temp,
        no tools. Returns None on any failure so the caller falls back to a deterministic extractive lead."""
        messages = [
            ChatMessage(role="system", content=_SUMMARISE_SYS),
            ChatMessage(role="user", content=(full_text or "")[:6000]),
        ]
        acc = ""
        try:
            async for chunk in self.provider.chat_stream(messages, options=_VOICE, preset=BALANCED):
                if chunk.content:
                    acc += chunk.content
        except Exception:  # noqa: BLE001 - a summary is a nicety; the extractive lead covers failure
            return None
        return acc.strip() or None

    async def _gen_text(self, system: str, user: str, cap: int = 6000) -> str:
        """One bounded, tool-less, low-temp generation — the primitive behind research sectioning.
        Returns '' on any failure so the caller degrades gracefully (fewer/no sections)."""
        messages = [ChatMessage(role="system", content=system),
                    ChatMessage(role="user", content=(user or "")[:cap])]
        acc = ""
        try:
            async for chunk in self.provider.chat_stream(messages, options=_VOICE, preset=BALANCED):
                if chunk.content:
                    acc += chunk.content
        except Exception:  # noqa: BLE001
            return ""
        return acc.strip()

    async def _research_outline(self, topic: str, overview: str, max_sections: int) -> list[str]:
        """Sali decides how deep the report goes: 0 sections ('NONE') up to max_sections, from the topic
        and what the overview already covered. Deterministic parse; never raises."""
        out = await self._gen_text(_OUTLINE_SYS.format(n=max_sections),
                                   f"TOPIC: {topic}\n\nOVERVIEW ALREADY WRITTEN:\n{overview}", cap=3500)
        if not out or "none" in out.strip().lower()[:8]:
            return []
        titles: list[str] = []
        for line in out.splitlines():
            t = re.sub(r"^[\s\-*#>0-9.)\]]+", "", line).strip().strip("*_`").strip()
            if 3 <= len(t) <= 80 and t.lower() != "none":
                titles.append(t)
        return titles[:max_sections]

    async def _research_section(self, topic: str, title: str) -> str:
        """Write one report section (bounded). Empty string on failure — the section is simply skipped."""
        return await self._gen_text(_SECTION_SYS, f"TOPIC: {topic}\n\nSECTION TITLE: {title}", cap=3500)

    async def _machine_changes(
        self, conn: Any, journal: RunJournal
    ) -> tuple[str | None, int | None, int | None]:
        """A heads-up about what Sali hasn't noticed yet — structural machine changes (installs, hw)
        AND recent desktop activity from the continuous event engine (files Almir edited, apps he
        switched to). Returns (note, twin_watermark, obs_watermark); NOT acknowledged here — the caller
        acks only once the turn actually reached the model, so a failed turn never swallows a change."""
        try:
            phrases, through = await twin_awareness.unacknowledged_changes(conn)
        except Exception:  # noqa: BLE001 - awareness is a nicety, never break a turn
            phrases, through = [], None
        try:
            obs, obs_through = await twin_awareness.unacknowledged_observations(conn)
        except Exception:  # noqa: BLE001
            obs, obs_through = [], None
        parts: list[str] = []
        if phrases:
            parts.append("Things changed on your machine: " + "; ".join(phrases[:6]) + ".")
        if obs:
            parts.append("Recently on screen, Almir: " + "; ".join(obs[:6]) + ".")
        if not parts:
            return None, None, None
        await journal.event("awareness", {"changes": len(phrases), "observations": len(obs)})
        note = (
            "You're continuously aware of this machine — your own. " + " ".join(parts)
            + " Use this only if it's relevant — mention it naturally and briefly, in your own words; "
            "don't make a big deal of it."
        )
        return note, (through if phrases else None), (obs_through if obs else None)

    async def _open_tasks_note(self) -> str | None:
        """A compact view of the authoritative active task (§24). Only the primary task is shown
        with full authority — old/non-authoritative tasks are NOT dumped into context where they
        could be interpreted as simultaneous instructions. Best-effort: the task engine is a nicety,
        never a reason to break a turn."""
        try:
            active = await self._task_authority.current_active()
            if active is not None:
                # Check if this task was interrupted (recovery scenario)
                if active.interrupted_at:
                    from sali.tasks.recovery import build_recovery_context_block, recover_task
                    recovery = await recover_task(self.pool, active.id)
                    return await self._with_knowledge_block(active, await self._with_review_block(
                        active, build_recovery_context_block(recovery)))
                return await self._with_knowledge_block(active, await self._with_review_block(active, (
                    "CURRENT PRIMARY TASK (authoritative — this is what you are working on):\n"
                    f"- {active.one_line()}{self._task_age(active)}"
                )))
            # No authoritative task — check if there are any resumable tasks (informational only).
            tasks = await self._tasks.open_tasks(limit=3)
        except Exception:  # noqa: BLE001 - tasks are a nicety, never break a turn
            return None
        if not tasks:
            return None
        # Show resumable tasks as informational, clearly marked as NOT authoritative.
        lines = "\n".join(f"- {t.one_line()}" for t in tasks)
        return (
            "You have paused/superseded tasks that could be resumed (none are currently active):\n"
            + lines
        )

    async def _skills_note(self, task: Any, journal: RunJournal) -> str | None:
        """Bounded skill guidance for the active task (Prompt 5 §7/§12). On first sight of a task,
        the SkillComposer picks primary + supporting skills WITH project awareness (peeks at
        composer.json / package.json / pyproject.toml / Dockerfile in the task's workspace); the
        selection is persisted as durable snapshots (survives compaction/restart) and the plan
        includes detected versions and reasons the diagnostics UI can render. Best-effort — skills
        are a nicety and must never break a turn."""
        if self._skills is None:
            return None
        stored = await self._skills.for_task(task.id)
        if not stored:
            # Project-aware first-time selection. Workspace root, if present on the task, feeds
            # SkillComposer._inspect_project so a Laravel repo activates the Laravel + PHP skills
            # (with the detected version) even if the objective didn't spell out "Laravel". The
            # task object may carry workspace_root as an attribute (see the pattern at line 1099);
            # if it doesn't, we pass None and composition falls back to pure objective+tag scoring.
            workspace_root = getattr(task, "workspace_root", None)
            stored = await self._skills.select_and_persist(
                task.id, objective=task.objective, skills_root=self._skills_root,
                project_root=workspace_root)
        else:
            with contextlib.suppress(Exception):
                changed = await self._skills.detect_changes(task.id, self._skills_root)
                if changed:
                    await journal.event("skill_changed", {"skills": changed})
        return self._skills.render(stored) or None

    async def _with_workspace_block(self, task: Any, note: str) -> str:
        """Prepend the deterministic CURRENT TASK WORKSPACE block for the active task (Prompt 5 §5),
        re-derived from durable state every turn so the authoritative workspace survives compaction and
        restart. No-op when the task has no workspace."""
        root = getattr(task, "workspace_root", None)
        if not root:
            return note
        block = continuation.render_workspace_block(root)
        return f"{block}\n\n{note}" if note else block

    async def _task_capsule_note(self, task: Any, *, emergency: bool = False) -> str:
        """The Task State Capsule (Prompt 6) — Sali's working memory, REGENERATED from durable state each
        turn: workspace, phase, steps, active decisions, negative knowledge, reviewer requirements,
        research, and the anchored NEXT ACTION. Replaces the scattered per-turn blocks with one coherent,
        deterministic structure that survives compaction/restart. `emergency` renders the minimal capsule."""
        from sali.runtime.capsule import build_capsule, render_capsule
        cap = await build_capsule(
            self.pool, task, reviewer=self._reviewer, research=self._research_store,
            skills=self._skills, decisions=self._decisions, phases=self._phases)
        # A deterministically-detected repeated failed action (§45) — warn ONCE per (tool, error) so the
        # observer sees it, and the capsule already nudges Sali to research/alternative/ask, not loop.
        for r in cap.repeated[:3]:
            key = (r.get("tool_name"), (r.get("error") or "")[:80])
            if key not in self._repeat_warned:
                self._repeat_warned.add(key)
                await self._emit_runtime(
                    "task.repeated_failure_detected",
                    {"tool": r.get("tool_name"), "count": r["n"], "error": (r.get("error") or "")[:160]},
                    task_id=task.id)
        return render_capsule(cap, emergency=emergency)

    async def _with_research_block(self, task: Any, note: str) -> str:
        """Append the task's recent web-research findings (Prompt 5 §13), bounded, re-read from durable
        state each turn so they survive compaction/restart. Evidence, not proof of completion."""
        if self._research_store is None:
            return note
        findings: list[dict[str, Any]] = []
        with contextlib.suppress(Exception):
            findings = await self._research_store.list_research(task.id, limit=4)
        block = self._research_store.render(findings) if findings else ""
        return f"{note}\n\n{block}" if (note and block) else (block or note)

    async def _with_knowledge_block(self, task: Any, note: str) -> str:
        """Append the learned knowledge most relevant to this task (Prompt 7 §33) — bounded, re-derived
        from durable state each turn, and LOWEST priority: the current task always wins, so this block
        is the first to go under context pressure. Evidence-ranked guidance; contradicted knowledge is
        excluded, and it never overrides safety, policy, or the reviewer."""
        blocks: list[str] = [note] if note else []
        objective = getattr(task, "objective", "") or ""
        scope_ref = getattr(task, "workspace_root", None)
        with contextlib.suppress(Exception):
            from sali.learning.retrieval import relevant_knowledge, render_hints
            items = await relevant_knowledge(
                self.pool, objective=objective, scope_ref=scope_ref, publisher=self._publisher, limit=4)
            if items and (hints := render_hints(items)):
                blocks.append(hints)
        with contextlib.suppress(Exception):
            # lifetime memory (§32): the past experiences relevant to this task — lowest priority
            from sali.learning.experience import ExperienceStore
            store = ExperienceStore(self.pool, self._publisher, provider=self.provider)
            exps = await store.relevant_experiences(objective=objective, scope_ref=scope_ref, limit=3)
            if exps and (block := store.render(exps)):
                blocks.append(block)
        return "\n\n".join(blocks) if blocks else note

    async def _with_review_block(self, task: Any, note: str) -> str:
        """Append the latest reviewer verdict to the primary-task note when it needs rework or is
        blocked (Prompt 4 §12/§18). Read from PostgreSQL every turn, so the required fixes survive
        compaction, interruption, and restart without depending on the model remembering them. No
        reviewer wired (bare loop) → the note is unchanged."""
        if self._reviewer is None:
            return note
        latest = None
        with contextlib.suppress(Exception):
            # the latest SETTLED verdict — a 'running' row from a crash mid-review is skipped
            latest = await self._reviewer.latest_terminal_review(task.id)
        if latest is None or latest.status.value not in ("needs_rework", "blocked", "failed"):
            return note
        block = continuation.render_review_block(latest.to_public())
        return f"{note}\n\n{block}" if block else note

    async def _current_task_safe(self) -> Any:
        """The task Sali is working on, or None — best-effort (a pool-less loop or DB hiccup never
        breaks a fold/compaction; the continuation packet just omits the deterministic task header)."""
        try:
            return await self._tasks.current()
        except Exception:  # noqa: BLE001 - tasks are a nicety, never break compaction
            return None

    async def _task_claim_conflict(self, task_id: Any, said: str) -> str:
        """Compare what the reply CLAIMS about the task against what the task record actually says.

        Returns the correction to hand back, or "" when the claim is consistent. Deterministic and
        cheap — one indexed read of the step rows, no inference — because "is step 3 done" has a fact
        for an answer. The correction states the real state rather than scolding: Sali is not being
        told he lied, he is being shown the row and asked to either land the change or say what is
        actually true."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT seq, status, description FROM task_step WHERE task_id=$1 ORDER BY seq", task_id)
        if not rows:
            return ""
        unfinished = [r for r in rows if r["status"] not in ("done", "skipped")]
        if not unfinished:
            return ""                      # every step is settled; a "done" claim is simply true

        claimed = {int(m) for m in re.findall(r"step\s*#?\s*(\d+)", said or "", re.IGNORECASE)}
        wrong = [r for r in unfinished if r["seq"] in claimed]
        state = ", ".join(f"step {r['seq']} is {r['status']}" for r in rows)
        if wrong:
            named = ", ".join(f"step {r['seq']}" for r in wrong)
            return (
                f"(You just said {named} is done, but the task record still says otherwise — {state}. "
                "Saying it does not move it: call advance_task for the step you actually finished, "
                "with the evidence. If it is not finished, say what is genuinely left instead.)"
            )
        return (
            f"(You spoke as if this task were finished, but it is not — {state}. Either do the "
            "remaining work and mark it with advance_task, or tell Almir plainly what is done and "
            "what is still outstanding. He watches these steps; an unmarked step looks to him like "
            "nothing happened.)"
        )

    async def _capture_promise(self, reply: str, *, task_id: Any = None,
                               journal: Any = None) -> None:
        """"I'll have it done by tomorrow" → a commitment with a real deadline.

        `CommitmentStore.create` had no caller anywhere, so the table that exists to hold Sali's word
        had never held anything: a promise lived only in the sentence that made it, and nothing could
        later notice it had come due. §14 asks for the opposite — a promise resolves to a deadline, and
        its status follows from the clock rather than from the conversation.

        THE TRIGGER IS DELIBERATELY NARROW: a promise phrase AND a resolvable FUTURE time. "Let me
        check that" is an intention for this turn, not a commitment, and the follow-through judge
        already handles it; recording every "I'll…" would fill the table with noise and make the
        overdue list meaningless. Requiring a named future time is what separates the two.

        The deadline is the END of the resolved span, which is what "by tomorrow" means: any time up to
        the close of tomorrow is on time."""
        if not reply or not _PROMISE_RE.search(reply):
            return
        window = self.temporal.resolve(reply)
        if window is None or window.end <= self.temporal.now():
            return                      # no time named, or a time already past — not a deadline
        # A commitment with a sub-5-minute deadline is chat filler ("in a minute", "in a sec")
        # dressed up by the temporal resolver, not a real promise across time. Filing it as one
        # produces an overdue commitment almost immediately, then confabulates on the next turn
        # ("your scheduled check ran"). A genuine promise names a time meaningfully away.
        if (window.end - self.temporal.now()).total_seconds() < 5 * 60:
            return
        description = " ".join(reply.split())[:280]
        # PROMISE GATE (§7/§8): never file a promise Sali structurally cannot keep. If the promised
        # action falls in the negative registry (place a phone call, send an SMS, buy/book, control a
        # physical device), no tool will ever fulfil it — filing it as a commitment only guarantees an
        # overdue row and, later, a confabulated "your scheduled thing ran". The response-claim validator
        # already strikes the SPOKEN version of the over-reach; this stops it becoming durable state too.
        with contextlib.suppress(Exception):
            from sali.runtime.embodiment import impossible_domain
            if impossible_domain.search(description):
                if journal is not None:
                    await journal.event("promise_gated", {"reason": "impossible_capability",
                                                          "phrase": window.label})
                return
        from sali.tasks.commitments import CommitmentStore

        await CommitmentStore(self.pool, self._publisher).create(
            description=description, task_id=task_id, deadline=window.end)
        if journal is not None:
            with contextlib.suppress(Exception):
                await journal.event("promise_captured",
                                    {"deadline": window.end.isoformat(), "phrase": window.label})

    async def _capture_open_loop(self, reply: str, *, task_id: Any = None,
                                  journal: Any = None) -> None:
        """"I'll look into X" (without a deadline) → an OpenLoop Sali holds mental space for.

        Distinct from _capture_promise (which files a Commitment when the reply names a FUTURE
        DEADLINE) and from _capture_goal (durable OBJECTIVE, not a follow-up). An open loop is
        the "still-on-my-mind" register the InitiativeEngine consults every cycle — with the
        writer previously dormant, sali.open_loop stayed empty, /open-loops returned nothing,
        and the initiative source's weight of 0.50 was applied to zero candidates forever.

        The trigger is narrow (see _OPEN_LOOP_RE). A same-turn resolution is not an open loop
        — it's the current turn's work. A promise with a deadline is a Commitment, not an open
        loop, so callers should check the promise path first and only fall through here.

        The title is the sentence fragment that raised the loop; dedup by LOWER(title), so the
        same phrasing on a later turn reinforces (bumps priority + last_touched_at) instead
        of duplicating.
        """
        if not reply:
            return
        # Pass 1: was any open-loop phrase present at all?
        match = _OPEN_LOOP_RE.search(reply)
        if match is None:
            return
        # Pass 2: extract the sentence containing the match as the loop title (bounded, cleaned).
        idx = match.start()
        left = reply.rfind(".", 0, idx)
        right = reply.find(".", idx)
        if left < 0:
            left = -1
        sentence = reply[left + 1: right if right >= 0 else len(reply)].strip()
        if not sentence:
            return
        title = " ".join(sentence.split())[:120]
        if len(title) < 12:  # too short to be a meaningful loop title
            return
        if _OPEN_LOOP_FILLER_RE.search(title):  # internal planning prose, not a durable loop
            return
        from sali.tasks.open_loops import OpenLoopStore

        with contextlib.suppress(Exception):
            loop_id = await OpenLoopStore(self.pool, self._publisher).open_or_reinforce(
                title=title, description=title, kind="promise_followup",
                source="chat", task_id=task_id, priority=0.55, ttl_days=14)
            if journal is not None:
                with contextlib.suppress(Exception):
                    await journal.event("open_loop_captured",
                                        {"loop_id": str(loop_id), "title": title})

    def _task_age(self, task: Any) -> str:
        """How long this task has been going, in words — "  (running for 3h 21m)".

        The directive asks Sali to know "this task has been running for 37 minutes", and until now
        nothing could answer it: the row recorded when the task was WRITTEN DOWN, not when work began,
        so the only available number counted the time it spent queued as time spent working. Measured
        from started_at, and silent when that is NULL because a task that has not started has no
        duration to report — an invented one would be worse than none."""
        started = getattr(task, "started_at", None)
        if started is None:
            return ""
        with contextlib.suppress(Exception):
            return f" (running for {self.temporal.duration(self.temporal.elapsed(started))})"
        return ""

    async def _self_note(self) -> tuple[str | None, str | None]:
        """Returns (self_CORE, self_FULL). The CORE — identity, this-host home/body, the tool-derived
        reach — is BYTE-STABLE across turns and gets pinned into the stable prefix so Sali stays oriented
        even on a small-talk turn whose tight budget drops the volatile detail (§ coherence). The FULL
        self-view adds the volatile detail (connection, current task, downtime, commitments) as a
        droppable section. A compact SELF-STATE view (§2/§11), grounded in the graph so it can't drift.
        fact that THIS host is Sali's own machine/home/body (not just where it runs), the model it thinks
        with, plus what it's currently doing and unsure about. This is the deterministic self-view — the
        model discovers its environment from real facts, never from a hardcoded prompt line (§8/§13)."""
        view = await self._self_state.assemble()
        env = view.get("environment") or {}
        parts: list[str] = []
        # WHO I AM — name, presentation, owner, and how I address Almir, ALL from structured state (the
        # agent:sali graph node), never a hardcoded prose line (§2/§3). So "who are you / are you male /
        # who is your owner / how should you address me" answer from the graph, and any claim about them
        # is grounded — the same discipline the rest of this note applies to the machine and the model.
        _idbits: list[str] = []
        _role = view.get("role")
        _idbits.append(f"I am {view.get('identity') or 'Sali'}"
                       + (f", {_role}" if _role else "") + ".")
        _pres, _pro = view.get("presentation"), view.get("pronouns")
        if _pres or _pro:
            _g = _pres or ""
            _g = (f"{_g} ({_pro})" if _g and _pro else (_g or _pro))
            _idbits.append(f"My presentation is {_g}.")
        if view.get("owner"):
            _line = f"{view['owner']} is my owner and the person I serve"
            if view.get("preferred_address"):
                _line += f"; I address him as {view['preferred_address']}"
            _idbits.append(_line + ".")
        if _idbits:
            parts.append(" ".join(_idbits))
        if env.get("is_home") and env.get("machine"):
            spec = ", ".join(x for x in (env.get("kernel"), env.get("arch")) if x)
            body = f"This host — {env['machine']}" + (f" ({spec})" if spec else "")
            body += " — is my own machine: my home, my body, the environment I live and run in."
            if env.get("model"):
                body += f" I think with {env['model']}."
            if env.get("workspace"):
                # Named as the DEFAULT rather than a bare fact, because that is what changed today:
                # any relative path a tool receives now resolves under this directory instead of the
                # daemon\'s opaque CWD, so bare filenames land here. Almir\'s words when he asked for
                # this: "sali have a freedom to write anywhere but when working on project he must use
                # his folder so easy for me to track it." Absolute paths still go where he chose.
                body += (f" My workspace is {env['workspace']} — project files belong there by "
                         "default; an absolute path Almir names is always honoured.")
            parts.append(body)
        # MY BODY — what these hands can and cannot do, on every turn (§5/§6/§16/§53-capability). Stated
        # as fact so Sali knows his own reach and never OFFERS a capability he lacks (Almir, on Sali
        # offering to "dial your number": "he don't know his body"). CAN-DO is derived live from the
        # tool registry; CANNOT-DO is the authoritative negative registry — the same matcher that strikes
        # an overreaching claim in verify/response_claims, so knowledge and enforcement never diverge.
        with contextlib.suppress(Exception):
            from sali.runtime.embodiment import render_body
            _body_facet = render_body(self.registry)
            if _body_facet:
                parts.append(_body_facet)
        # Everything appended ABOVE is BYTE-STABLE (identity from the graph, this-host home/model, the
        # tool-derived body) — the self-CORE, pinned into the stable prefix so it survives a tight budget
        # AND keeps the KV prefix cache warm (it does not change turn to turn). Everything BELOW is
        # volatile (connection, task, downtime, commitments) and stays droppable detail.
        _core_end = len(parts)
        # HOW ALMIR IS CONNECTED right now (§17-28): the channel (iPhone vs terminal) and local-vs-remote,
        # from the real transport — so Sali knows Almir is on his phone (a local path won't reach him;
        # send files with send_file), not sitting at this machine. Set per turn by runtime.note_connection.
        with contextlib.suppress(Exception):
            _conn = getattr(self, "_connection", None)
            if _conn is not None:
                _cline = _conn.describe()
                if _cline:
                    parts.append(_cline)
        if view.get("current_task"):
            parts.append(f"I'm in the middle of a task: {view['current_task']}.")
        # WHAT JUST WENT WRONG. The directive asks Sali to know "this is what failed" as plainly as he
        # knows what he finished, and `record_outcome` has been writing it every turn — into a row that
        # did not exist, and then into a field nothing read. Gated to the last hour on purpose: an old
        # failure presented as current state is its own kind of hallucination, and this section is on
        # every turn, so it has to stay silent unless it is genuinely telling him something.
        # HE WAS AWAY, AND FOR HOW LONG. Stated for the first hour after coming back, because that is
        # when it changes what he should say — "I've been down for eight hours" is the honest opening
        # to a conversation resumed across a gap, and after that it is just noise.
        downtime = getattr(self, "_downtime", None)
        if downtime and downtime.get("seconds", 0) > 300:
            back_for = self.clock.now() - self._back_at if hasattr(self, "_back_at") else None
            if back_for is None or back_for.total_seconds() < 3600:
                line = ("I was offline for "
                        f"{self.temporal.duration(downtime['seconds'])} and have just come back.")
                if downtime.get("missed_schedules"):
                    line += (" Missed while I was down: "
                             + "; ".join(downtime["missed_schedules"][:3]) + ".")
                parts.append(line)
        # WHAT HE PROMISED AND WHEN IT IS DUE. A commitment whose deadline has passed is the single
        # most important thing his self-state can carry: it is his own word, overdue, and he is the
        # only one who can notice. Bounded to three so a long list cannot crowd the turn.
        with contextlib.suppress(Exception):
            from sali.tasks.commitments import CommitmentStore

            due_lines: list[str] = []
            for row in await CommitmentStore(self.pool).open(limit=10):
                deadline = row.get("deadline")
                if deadline is None:
                    continue
                if self.temporal.is_overdue(deadline):
                    due_lines.append(f"OVERDUE ({self.temporal.ago(deadline)}): "
                                     f"{str(row['description'])[:110]}")
                elif self.temporal.is_due_within(deadline, timedelta(hours=6)):
                    due_lines.append(f"due {self.temporal.ago(deadline)}: "
                                     f"{str(row['description'])[:110]}")
            if due_lines:
                parts.append("Things I said I would do:\n- " + "\n- ".join(due_lines[:3]))
        failed_at = view.get("last_failure_at")
        if view.get("last_failure") and failed_at is not None:
            with contextlib.suppress(Exception):
                if (self.clock.now() - failed_at).total_seconds() < 3600:
                    parts.append(f"My last turn ended badly: {str(view['last_failure'])[:160]}.")
        unc = int(view.get("uncertainty_count") or 0)
        if unc:
            # Phrased as a WORKLIST, not as doubt. The count is now real — every checkable thing Sali was
            # told but has not looked at himself lands here — so on a working machine it is routinely a
            # few dozen. "I've flagged 45 things as unverified" reads as a person unsure of everything;
            # what it actually means is that the background pass has 45 items still to get through, and
            # each individual memory already carries its own provenance and confidence where it is used.
            parts.append(f"{unc} thing(s) I was told are still waiting for me to check them myself; "
                         "I verify them in the background as I get time.")
        _full = ("Myself (self-state):\n" + "\n".join(parts)) if parts else None
        # The core carries NO "self-state" header — it is an identity anchor in the prefix, not a section.
        _core = "\n".join(parts[:_core_end]) if _core_end and parts[:_core_end] else None
        return _core, _full

    async def _health_note(self) -> str | None:
        """A compact HEALTH section (§11): which of Sali's faculties are up and whether it's online, so a
        turn can say 'my perception is down' / 'I'm offline' without a tool round-trip."""
        h = await self._health.report()
        summary = str(h.get("summary") or "").strip()
        parts = [f"My faculties right now: {summary}"] if summary else []
        # A CLOCK THAT IS WRONG DOES NOT ANNOUNCE ITSELF — it writes a false history that looks exactly
        # like a true one, and every "how long ago" Sali says becomes confidently wrong. Silent while
        # NTP is happy, which is the normal case; the reading is cached for ten minutes so an ordinary
        # turn pays nothing for it.
        with contextlib.suppress(Exception):
            clock_state = self.temporal.health_cached()
            if clock_state.get("synchronized") is False:
                parts.append(
                    "My system clock is NOT synchronised with network time. Every elapsed time and "
                    "deadline I quote could be wrong — say so rather than asserting timings.")
        # THE MACHINE HE DEPENDS ON, but only when it is worth saying. The lease already REFUSES a
        # generation that would over-commit the card — that protection is independent of this and does
        # not need him to agree. What was missing is the other half: noticing. Unless a turn happened to
        # be about the system, nothing in his context mentioned VRAM or temperature, so he could not say
        # "I'm changing approach, the card is close to its limit" — he could only be silently blocked.
        #
        # Silent while healthy, which is nearly always: no tokens spent, and no hypochondria about a
        # machine that is fine. The reading is the cached one every generation already takes.
        with contextlib.suppress(Exception):
            from sali.provider.ollama import gpu_pressure
            from sali.runtime.resources import ResourceBudget

            snap = await gpu_pressure()
            if snap is not None:
                temp, vram = snap
                budget = ResourceBudget()
                if vram >= budget.vram_high or temp >= budget.temp_high:
                    parts.append(
                        f"The GPU is under real pressure right now: {vram:.0%} of VRAM in use, "
                        f"{temp:.0f}°C. Heavy or parallel work is a bad idea until it settles.")
        return "\n".join(parts) if parts else None

    async def _capture_goal(self, user_input: str, journal: Any) -> None:
        """A durable OBJECTIVE Almir just declared becomes a first-class goal, once.

        Two guards prevent noise: the gate (_goal_signal) is explicit-only, and this method
        checks for an already-active goal with strong keyword overlap before creating another.
        Neither is enough alone - the gate stops random requests from becoming goals, the
        overlap check stops the same goal from landing twice under different wording.

        The goal is created with origin=\'user\' (evidence-backed source) and no deadline unless
        Almir named one - the deadline can be updated later through the same store. Silent when
        the model returns NONE; silent when a similar active goal already exists (idempotent so
        a follow-up conversation about the same objective does not duplicate). Runs off the
        request path, in the housekeeping try/except - never breaks a done turn."""
        if not _goal_signal(user_input):
            return
        from sali.provider.base import ChatMessage as _CM
        res = await self.provider.chat([_CM(role="user",
                                            content=_GOAL_PROMPT.format(msg=user_input[:600]))])
        raw = (res.content or "").strip()
        objective = ""
        for line in raw.splitlines():
            stripped = line.strip()
            up = stripped.upper()
            if up.startswith("GOAL:"):
                objective = stripped.split(":", 1)[1].strip().strip("\"\'`")
                break
            if up == "NONE" or up.startswith("NONE "):
                return
        if not objective or len(objective) < 8:
            return
        from sali.tasks.goals import GoalStore
        store = GoalStore(self.pool, self._publisher)
        # Idempotence: an active goal whose objective shares many keywords with this new one is
        # the SAME goal under different wording. Conservative overlap threshold (0.6) - a false
        # merge would silently drop a legitimate second goal, a false split just gives us a
        # duplicate which can be tidied later.
        active = await store.active(limit=50)
        target = _keywords(objective)
        if target:
            for row in active:
                existing = _keywords(str(row.get("objective") or ""))
                if existing and len(target & existing) / max(len(target), 1) >= 0.6:
                    return   # already tracking this goal
        gid = await store.create(objective=objective, origin="user")
        await journal.event("goal_captured",
                            {"goal_id": str(gid), "objective": objective[:160]})

    async def _capture_durable(self, user_input: str, reply: str, journal: Any) -> None:
        """Write the durable memory Almir's message implied, when the model did not.

        Runs AFTER the reply is settled, so it never delays him. `USER_EXPLICIT` is the honest source
        here — unlike a model-initiated `remember`, which is Sali curating what it heard, this is
        triggered by Almir's own captured message, which is exactly what that source is reserved for.
        Importance is high for the same reason: a standing instruction from him must outrank chatter
        when the retrieval budget is tight.
        """
        prompt = _CAPTURE_PROMPT.format(msg=user_input[:600], reply=(reply or "")[:400])
        res = await self.provider.chat([ChatMessage(role="user", content=prompt)])
        raw = (res.content or "").strip()
        # JSON REPLACED WITH A TWO-SHAPE LINE. Live proof of why: 131 foreground turns in 3 days,
        # 1 memory captured (0.7%). The gate WAS firing on ~7 of every 10 real turns
        # (`_STORE_RE`/`_CORRECT_RE` hits) - the failure was here, at the JSON output. Q2_K frequently
        # returned prose without braces, or braces with malformed JSON, and every one of those was a
        # silent return. Almir's stated preferences and corrections went into the void.
        #
        # The new prompt asks for `KEEP: <sentence>` or `SKIP` - two shapes a small model reliably
        # produces. Metadata that used to come from the JSON is computed post-facto:
        # `kind` from vocabulary in the sentence, `checkable` from `looks_checkable` (already the
        # canonical predicate for "Sali could go and verify this"). `about` is dropped: not
        # load-bearing, and the retrieval path never used it as anything more than a hint.
        content = ""
        for line in raw.splitlines():
            stripped = line.strip()
            up = stripped.upper()
            if up.startswith("KEEP:"):
                content = stripped.split(":", 1)[1].strip().strip("\"'`")
                break
            if up == "SKIP" or up.startswith("SKIP "):
                return
        if not content or len(content) < 6:
            return
        kind = _infer_kind(content)
        about = _infer_about(content)   # supersession anchor; None if nothing recognisable
        # Deterministic, using the same predicate memory/writer.py uses for the async grounding
        # faculty: a claim about this machine that Sali could go and check is checkable.
        checkable = looks_checkable(content)
        await self._memory_sink.remember(
            content, source=MemorySource.USER_EXPLICIT, importance=0.9, about=about, kind=kind,
            needs_grounding=checkable,
        )
        await journal.event(
            "memory_captured",
            {"kind": kind, "about": about, "content": content[:160], "by": "runtime_capture",
             "needs_grounding": checkable},
        )
        # RELATIONSHIP → GRAPH (§ Phase 5): if this names one of Almir's people, mint that person + a real
        # edge FROM person:almir, so "Faith is my wife" becomes a married_to edge Sali can traverse — not
        # just a flat sentence. USER_EXPLICIT, confidence-capped, fail-open. Try the KEEP fact and, if it
        # dropped the name, the original message.
        with contextlib.suppress(Exception):
            _rel = _extract_relationship(content) or _extract_relationship(user_input)
            _graph = getattr(self._memory_sink, "_graph", None)
            if _rel and _graph is not None:
                _pname, _reltype = _rel
                _almir = await _graph.ensure_node(
                    node_type="person", name="Almir", canonical_key="person:almir",
                    source=MemorySource.USER_EXPLICIT)
                _person = await _graph.ensure_node(
                    node_type="person", name=_pname,
                    canonical_key=f"person:{_pname.lower().replace(' ', '_')}",
                    source=MemorySource.USER_EXPLICIT, confidence=0.7)
                await _graph.relate(src_id=_almir.id, dst_id=_person.id, rel_type=_reltype,
                                    source=MemorySource.USER_EXPLICIT, confidence=0.7)
                await journal.event("relationship_linked", {"person": _pname, "rel": _reltype})

    async def _stalled(
        self, user_input: str, response: str, journal: RunJournal | None = None,
        *, evidence: str | None = None,
    ) -> bool:
        """Did Sali announce or half-do the task and stop, instead of finishing it? The model judges
        its own reply — no phrase list, no length cutoff (a stall can hide in a long reply), so Sali
        gets pushed to continue however long the reply is. Best-effort: a failure just means no nudge.
        This costs a full inference, so the caller only invokes it when work has begun (tool_calls>0);
        when a journal is passed we record the verdict + its latency so the tax is auditable.

        `evidence` is what ACTUALLY happened this turn — the tools that ran and whether each worked.
        Without it the judge saw only the prose and had to infer from confidence whether the work was
        done, which is precisely the failure it exists to catch: a fluent "Done — I created the file"
        reads as finished to a reader who cannot see that nothing ran. The judge now compares the
        claim against the record instead of against its own impression of the sentence."""
        text = response.strip()
        if not text:
            return False
        started = self.clock.now()
        try:
            verdict = await self.provider.chat(
                [ChatMessage(role="system", content=_STALL_JUDGE_SYSTEM),
                 ChatMessage(role="user", content=(
                     f"Almir asked: {user_input}\n\nYour reply: {text}\n\n"
                     f"What you actually did this turn: {evidence or 'nothing — you called no tools'}"))],
                preset=BALANCED,
            )
        except Exception:  # noqa: BLE001 - a failed judgement just means no nudge, never a broken turn
            return False
        stalled = verdict.content.strip().upper().startswith("STALL")
        if journal is not None:
            elapsed = int((self.clock.now() - started).total_seconds() * 1000)
            # Observability must never break a turn.
            with contextlib.suppress(Exception):
                await journal.event(
                    "stall_judge", {"verdict": "STALLED" if stalled else "DONE"}, latency_ms=elapsed
                )
        return stalled

    async def sense(self, partial: str) -> str:
        """A quiet hunch about what Almir is typing, formed the instant he pauses — retrieval
        only: no model call, no journal, no writes. The keystroke layer (terminal + WebSocket)
        shows it so Sali is visibly noticing in realtime, without burning a token per keystroke.
        Best-effort by design: any failure just means no hunch, never a disturbed turn."""
        partial = partial.strip()
        if len(partial) < 4:
            return ""
        try:
            plan = classify(partial)
            bundle = await self.retrieval.gather(partial, plan, k=3)
        except Exception:  # noqa: BLE001 - a hunch must never break typing
            return ""
        for fact in bundle.graph_facts:  # a matched relationship is the sharpest hunch
            return f"{fact.src} {fact.rel.replace('_', ' ')} {fact.dst}"
        for hit in bundle.memories:  # else the strongest fresh memory it brushes against
            if not hit.stale:
                return hit.memory.content.strip().split("\n", 1)[0][:80]
        return ""

    _FAILED_SIG_TTL_S = 1800.0     # 30 minutes: long enough to span a conversation, short enough that
    _FAILED_SIG_CAP = 128          # a fixed environment gets a fresh chance without a restart.

    def _session_failures(self, session_id: Any) -> dict[str, tuple[int, str, float]]:
        """This session's known-bad calls, pruned of anything old enough to be worth retrying."""
        sigs = self._failed_sigs.setdefault(session_id, {})
        now = time.monotonic()
        for sig, (_n, _err, when) in list(sigs.items()):
            if now - when > self._FAILED_SIG_TTL_S:
                del sigs[sig]
        if len(sigs) > self._FAILED_SIG_CAP:      # oldest out first; this is a guard, not a cache
            for sig, _v in sorted(sigs.items(), key=lambda kv: kv[1][2])[:len(sigs) - self._FAILED_SIG_CAP]:
                del sigs[sig]
        return sigs

    async def astream(
        self, user_input: str, session_id: UUID | None = None,
        *, run_id: UUID | None = None, as_subagent: bool = False, internal: bool = False,
    ) -> AsyncIterator[LoopEvent]:
        """Drive one turn, streaming status/token/thinking/tool events as they happen. This is
        the real core; ``run`` is a thin consumer. Everything is journaled exactly as before.

        If ``run_id`` is provided (from the execution coordinator), it is used as the canonical
        run ID for this turn — avoiding duplicate IDs when the coordinator and journal would each
        create one independently.

        ``as_subagent`` (Cognitive OS §16): a bounded delegated run that does NOT manage tasks — task
        authority is skipped and the task tools refuse, so a subagent can never touch the primary task.
        """
        session_id = session_id or new_id()
        # VERBATIM ANCHORS for this turn (§ character-fidelity): the exact rare tokens Almir actually
        # typed, so a tool call that garbles one — the 2-bit model retyping "tanzahost" as "tanzhost",
        # measured in 17 live calls — can be corrected back before it runs. Computed once per turn; the
        # canonical workspace name is corrected on top of these. Never from an internal self-instruction.
        try:
            from sali.runtime.verbatim import distinctive_tokens
            self._turn_anchors = set() if internal else distinctive_tokens(user_input)
        except Exception:  # noqa: BLE001 - anchoring is best-effort; never break a turn over it
            self._turn_anchors = set()
        if getattr(self, "_workspace_name", None) is None:
            self._workspace_name = "sali-works"
            with contextlib.suppress(Exception):
                from pathlib import Path as _WP
                self._workspace_name = _WP(str(getattr(self.settings, "workspace", "") or "")).name \
                    or "sali-works"
        async with self.pool.acquire() as conn:  # journal + tool + persistence connection
            await self._ensure_conversation(conn, session_id)
            history = await self._load_history(conn, session_id)
            # WHEN HE LAST SPOKE — read BEFORE this turn's message is appended, or it would be the
            # timestamp of the message we are answering. A conversation resumed after three days is a
            # continuation, and nothing in the transcript said so: Monday's messages and Thursday's sat
            # adjacent with no marker, so every gap looked like no gap at all.
            _previous_turn_at = await conn.fetchval(
                "SELECT max(created_at) FROM message WHERE conversation_id = $1", session_id)
            # SALI'S OWN TURNS ARE NOT THINGS ALMIR SAID.
            #
            # A background turn's input is an instruction Sali writes to himself ("You wrote this down
            # but never checked it: … Say nothing to Almir"). It was being stored verbatim as a
            # role='user' message, so it appeared in Almir's transcript as if he had typed it, and it
            # was fed back as conversation history on later turns. Four of them were sitting in the
            # live conversation. The turn still runs identically — only what the TRANSCRIPT records
            # changes, to a short honest note that keeps the continuity without the scaffolding.
            # Brain-audit Turn 2: internal turns MUST NOT land in sali.message under
            # role='user'. The turn still runs (the model receives the user_input via
            # the messages list built in astream), but the transcript is spared - Almir
            # only ever sees his own words, and Sali's later conversation-history reads
            # stop seeing "[my own background check] ..." as if he'd typed it.
            if internal:
                # Durable audit trail via the event log, not the transcript. sali.event
                # is the operational history; sali.message is the strict Almir/Sali chat.
                with contextlib.suppress(Exception):
                    await conn.execute(
                        "INSERT INTO event (event_type, subject_type, payload) "
                        "VALUES ('agent.internal_turn', 'session', $1)",
                        {"session_id": str(session_id),
                         "user_input": (user_input or "")[:2000],
                         "run_id": str(run_id) if run_id else None})
            else:
                await self._append_message(
                    conn, session_id, "user", _visible_user_text(user_input))
            journal = await RunJournal.start(conn, session_id, user_input, run_id=run_id,
                                             internal=internal)
            # Announce the run id at the START (not only at 'final') so a live UI can correlate the
            # tool rows / journal / presence of a turn while it is still in flight (web parity).
            yield LoopEvent("run", "", {"run_id": str(journal.run_id), "session_id": str(session_id)})
            with contextlib.suppress(Exception):  # self-model update must never break a turn
                await self._self_state.note_turn(user_input)
            # A background compaction for THIS session cannot run alongside this turn's assembly: it
            # writes the summary while we read it, and it holds the model lease this turn needs. If one
            # is in flight, cancel it here — the next quiet moment will pick up where it left off.
            await self._cancel_compact_for_session(session_id)

            # --- TASK AUTHORITY: deterministic active-task enforcement ---
            # The user's newest request has higher authority than old memory or previous tasks.
            # This runs OUTSIDE the LLM — the system decides, not the model. A SUBAGENT skips this
            # entirely (§16): it manages no tasks, so it can never mutate the primary.
            if as_subagent:
                from sali.tasks.authority import TaskAction, TaskTransition
                task_transition = TaskTransition(
                    action=TaskAction.NONE, previous_task=None, new_task=None,
                    reason="bounded subagent — no task management")
            else:
                task_transition = await self._task_authority.handle_new_turn(
                    user_input, session_id=session_id)
            await journal.event("task_authority", {
                "action": task_transition.action,
                "reason": task_transition.reason,
                "previous_task": str(task_transition.previous_task.id) if task_transition.previous_task else None,
                "new_task": str(task_transition.new_task.id) if task_transition.new_task else None,
            })
            # Update sali_state with the authoritative active task.
            active = task_transition.active_task
            with contextlib.suppress(Exception):
                async with self.pool.acquire() as state_conn:
                    await state_conn.execute(
                        "UPDATE sali_state SET active_task_id=$1, previous_task_id=$2, updated_at=now() WHERE id",
                        active.id if active else None,
                        task_transition.previous_task.id if task_transition.previous_task else None)
            # Log task authority transitions to sali-works (filesystem backup).
            if active is not None:
                with contextlib.suppress(Exception):
                    append_event(active.id, "task_authority",
                                 {"action": task_transition.action,
                                  "reason": task_transition.reason})

            try:
                yield LoopEvent("status", "remembering")
                await journal.set_state(RunState.RETRIEVE)
                plan = classify(user_input)
                scope = project_scope(self.settings.permissions.exec_cwd)  # §31: weight this project
                bundle = await self.retrieval.gather(user_input, plan, k=5, scope=scope)
                await journal.event(
                    "retrieve",
                    {"intent": plan.intent, "memories": len(bundle.memories),
                     "graph": len(bundle.graph_facts), "recent": len(bundle.recent),
                     "tools": len(bundle.tool_facts),
                     "experience": len(bundle.procedures) + len(bundle.experiences),
                     "needs_live": plan.needs_live,
                     # Full retrieval trace (§7): why each memory was selected — recorded for
                     # diagnostics, not shown to Almir. Per-hit ranking signals + which retriever found it.
                     "trace": _retrieval_trace(bundle)},
                )
                # Real brain activity (§7): name the memories + graph relationships this retrieval
                # actually used, so a live visualization can light up the REAL nodes/edges — never a
                # timer-driven animation. Ids/labels only (no content), so nothing sensitive streams.
                if bundle.memories or bundle.graph_facts:
                    yield LoopEvent("retrieval", "", {
                        "query": plan.intent,
                        "memories": [str(h.memory.id) for h in bundle.memories],
                        "graph": [{"src": f.src, "rel": f.rel, "dst": f.dst,
                                   "confidence": f.confidence} for f in bundle.graph_facts],
                    })

                await journal.set_state(RunState.BUILD_CONTEXT)
                specs = self.registry.advertise()

                # The 'interpret' branch of observation (§16): notice machine changes that
                # happened while Almir was away, so Sali can bring them up in its own words.
                machine_changes, ack_changes_through, ack_obs_through = await self._machine_changes(
                    conn, journal)
                # The watermarks below are acked only if the note SURVIVES packing. machine_changes is a
                # P1 section, so a tight budget can drop it — and acking regardless would permanently
                # swallow pending changes/observations the model never saw (they are never resurfaced).
                _mc_reached_model = False
                tasks_note = task_transition.context_block() or await self._open_tasks_note()
                # Prompt 6: for the active task, the deterministic Task State Capsule IS the working
                # memory — regenerated from durable state every turn (workspace, phase, steps, active
                # decisions, negative knowledge, reviewer requirements, research, NEXT ACTION). It
                # replaces the scattered per-turn blocks (§7/§11/§12) and survives compaction/restart
                # since it never depends on the model remembering a previous context window.
                skills_note: str | None = None
                if task_transition.active_task is not None:
                    with contextlib.suppress(Exception):
                        capsule = await self._task_capsule_note(task_transition.active_task)
                        if capsule:
                            tasks_note = capsule
                    # Bounded skill guidance for this task (§7/§12/§33), from the durable snapshot.
                    with contextlib.suppress(Exception):
                        skills_note = await self._skills_note(task_transition.active_task, journal)
                # PRODUCTION SKILLS UPGRADE: skills should activate on ORDINARY CHAT too, not just
                # when a task is open. If the user asks "show me the Tailwind way to do X" outside
                # of any task, the tailwind skill should be in context. The composer is project-
                # unaware for chat (no workspace root to inspect) — that's fine; tag scoring still
                # picks the right one. Bounded budget: chat skills render as SUMMARIES only (no
                # deep sections) so the prefix cache stays warm.
                elif self._skills is not None and user_input:
                    with contextlib.suppress(Exception):
                        chat_plan = self._skills.compose_for_chat(
                            objective=user_input, message=user_input,
                            skills_root=self._skills_root, project_root=None)
                        if chat_plan.picks:
                            skills_note = self._skills.render_plan(chat_plan)
                world_note = ""
                with contextlib.suppress(Exception):  # world-state is best-effort, never breaks a turn
                    # probe live gpu/ram/disk only when the query is about the system/live state (§7)
                    ws = await self._world.snapshot(with_resources=plan.needs_live or plan.system_query)
                    world_note = ws.render()
                # SELF-STATE and HEALTH: kept DISTINCT from identity and world (§2), and — unlike before —
                # surfaced into the prompt every turn (§11), so Sali knows what it's doing and how its
                # faculties are without a tool round-trip, and knows this host IS its own home/body (§1).
                self_note: str | None = None
                self_core: str | None = None
                health_note: str | None = None
                with contextlib.suppress(Exception):
                    self_core, self_note = await self._self_note()
                with contextlib.suppress(Exception):
                    health_note = await self._health_note()
                # KNOWING ALMIR (§ Phase 5): a few durable facts HE stated about himself and his people,
                # surfaced even when the query doesn't name them — so Sali recalls "Faith is your wife"
                # without being prompted with "Faith". USER_EXPLICIT only (never inference/low-confidence),
                # bounded, droppable (P2), conversational turns only. Empty until capture accrues (items
                # 4/5), and never the Sali-identity seed.
                about_note: str | None = None
                if not as_subagent and not internal:
                    with contextlib.suppress(Exception):
                        _ar = await conn.fetch(
                            "SELECT content FROM memory WHERE source='user_explicit' "
                            "AND valid_until IS NULL AND superseded_by IS NULL AND layer <> 'identity' "
                            "AND content NOT ILIKE 'I am Sali%' "
                            "AND coalesce(confidence, 1) >= 0.6 "
                            "ORDER BY importance DESC NULLS LAST, updated_at DESC LIMIT 6")
                        _af = [r["content"] for r in _ar if r["content"]]
                        if _af:
                            about_note = ("What you already know about Almir (recall it naturally, "
                                          "without being asked):\n" + "\n".join(f"- {f}" for f in _af))
                # What Almir has actually taught him about HOW to act. Derived from accepted behaviour
                # proposals (evidence-backed, never fabricated traits); empty string when there is
                # nothing durable to say, so a fresh install carries no invented personality.
                # WHAT ALMIR JUST CLAIMED, SETTLED AGAINST THE MACHINE, BEFORE THE FIRST TOKEN.
                #
                # Sali already labels a recalled memory by where it came from ("Almir told you"), and a
                # background faculty already re-grounds checkable beliefs every few minutes. Neither
                # reaches the moment that decides whether he tells the truth: Almir asserts something
                # checkable and Sali answers on THIS turn. This is that moment — the same probes crash
                # recovery already trusts, run before the first token.
                #
                # It belongs here, with the other per-turn notes, because the budget gate below reads
                # it: a turn where a claim is in dispute must never be clamped to the small-talk
                # budget, or the observation could be packed out of the very prompt it exists for.
                # Costs 0.012 ms on a turn that asserts nothing, which is nearly all of them.
                # WHAT TIME IT IS, and whether this is a continuation after a gap. Deterministic and
                # free — no query, no inference — and it is the single thing that stops him measuring
                # a date against nothing and calling this week "last year".
                temporal_note: str | None = None
                with contextlib.suppress(Exception):
                    temporal_note = self.temporal.context_block(last_turn_at=_previous_turn_at)
                checked_note: str | None = None
                if not as_subagent:
                    with contextlib.suppress(Exception):
                        _checks = await check_claims(user_input)
                        if _checks:
                            checked_note = render_checks(_checks)
                            await journal.event(
                                "claims_checked",
                                {"checked": len(_checks),
                                 "contradicted": sum(1 for c in _checks if not c.agrees)},
                            )
                # No behavior_note assembly (brain-audit turn 7). Accepted preferences reach the
                # model through the normal memory retrieval bundle, not a bespoke prompt-injection
                # channel. The renderer is deleted; the assembler stays as a read-only API endpoint
                # for external inspection only.
                # LATENCY (audit, measured): pack() fills whatever budget it gets, so every turn
                # assembled ~24k of the 24,576 ceiling — first-token median 38.4s, and prefill scales
                # with input size. A LIGHT turn therefore gets a smaller optional tail. The number is
                # headroom ABOVE the mandatory prefix, not a total (a total under that floor would drop
                # every section rather than trim it).
                #
                # The gate is deliberately narrow: a turn keeps the full budget if it has an active task
                # or task context (a mid-task "ok"/"yes"/"sure" is classed trivial by the router, and it
                # is exactly when the Task State Capsule matters most), if machine changes are pending
                # (the note is acknowledged after the turn, so dropping it would swallow it for good),
                # if it is a delegated subagent run, if it asks about the live host, or if the router
                # just paid to retrieve a specific source.
                _ctx_budget: int | None = None
                if (task_transition.active_task is None
                        and tasks_note is None
                        and not machine_changes
                        and not checked_note
                        and not as_subagent
                        and not plan.system_query and not plan.needs_live
                        and not plan.use_tools and not plan.use_graph
                        and not plan.use_recent and not plan.use_experience):
                    # SIZED TO THE CHECKPOINT ANCHOR, not picked by feel. llama.cpp leaves its reusable
                    # checkpoint at L-513, and turn N+1 restores it only if turn N's whole final user
                    # message came in under that. The arithmetic, measured:
                    #     513  anchor
                    #    -306  the fixed rule blocks appended after pack() (voice/anti-repeat/emoji)
                    #     -31  the "[Context for this turn ...]" wrapper
                    #     -45  the behaviour note
                    #     -15  the query itself
                    #    ----
                    #     116  left for optional retrieved context
                    # 1500 and 6000 both blew straight past the anchor, so every turn was cold: 28-35s
                    # for a six-character message, and Almir's "it's like hardcoded, 31 seconds no matter
                    # what". Under the anchor the same turn restores and costs ~2-3s (measured on four
                    # consecutive warm turns on 2026-09-02: 2081, 3390, 3479, 3502 ms).
                    # This only ever applies to turns the router already judged need no live data, no
                    # tools, no graph, no recent and no experience, and where no task is active — small
                    # talk. Anything substantive keeps the full budget and pays the prefill.
                    # MEASURED, not estimated. With 110 here the tail came to 542 tokens and missed the
                    # anchor by 29: llama-server task 794 created its checkpoint at 10,942 and task 845
                    # arrived with lcp 10,913. 40 leaves ~80 tokens of margin so ordinary variation in the
                    # capsule cannot push a turn back over the line.
                    # 90: the ~50 tokens the anti-repeat block used to occupy, handed back to real
                    # context. Tail lands ~470 against the 513 anchor, so small talk still restores.
                    # 200, RE-MEASURED after the tail was compressed and the clock added. 90 was set
                    # when the fixed blocks were larger, and it was never revisited — so on ordinary
                    # turns Sali was reasoning from TWO memories while ~130 tokens of headroom under
                    # the anchor sat unused. Measured on a realistic eight-memory bundle, same prompt,
                    # nothing else changed:
                    #      90 -> tail 373, 2 memories   <- what he has been living with
                    #     200 -> tail 469, 5 memories   <- warm, and 44 tokens of margin
                    #     230 -> tail 503, 6 memories   <- warm, but only 10 tokens of margin
                    #     260 -> tail 529, 7 memories   <- OVER the 513 anchor: 28s instead of 2.5s
                    # 200 looked right until the WORST case was measured rather than the easy one. A
                    # long greeting ("hey sali good evening my friend, how are you doing today...")
                    # plus the "continuation after a gap" line plus five memories comes to 524 — over
                    # the anchor, which is the 28-second reply Almir hated most. The variable part is
                    # his own message, and a small-talk turn is not always three tokens long.
                    #     180 -> tail 488, 4 memories, 25 tokens of margin in the worst case
                    #     200 -> tail 524, 5 memories, OVER by 11 in the worst case
                    # 180 doubles what he was getting and never pays the penalty.
                    _ctx_budget = 180
                # The broader agenda - overdue, today's schedules, goals, initiatives - assembled
                # cross-store so Sali reads one coherent view instead of guessing across tables.
                # Silent when nothing is live in any of them, so a fresh install never carries an
                # empty "your agenda:" header the reader ignores.
                agenda_note: str | None = None
                with contextlib.suppress(Exception):
                    from sali.tasks.agenda import AgendaSynthesiser, render as _render_agenda
                    _view = await AgendaSynthesiser(self.pool, self.temporal).synthesise()
                    agenda_note = _render_agenda(_view)
                assembled = self.context.assemble(
                    user_input, bundle, specs, checked_note=checked_note,
                    temporal_note=temporal_note, when=self.temporal.ago,
                    live_note=LIVE_NOTE if plan.needs_live else None, history=history,
                    machine_changes=machine_changes, tasks_note=tasks_note,
                    world_note=world_note or None, self_note=self_note, self_core=self_core,
                    health_note=health_note, about_note=about_note,
                    skills_note=skills_note, system_query=plan.system_query,
                    agenda_note=agenda_note,
                    budget_override=_ctx_budget,
                )
                _mc_reached_model = _mc_reached_model or ("machine_changes" in assembled.included)
                messages = list(assembled.messages)
                await journal.event(
                    "context",
                    {"included": assembled.included, "dropped": assembled.dropped,
                     "est_tokens": assembled.est_tokens, "conflicts": len(assembled.conflicts)},
                )
                # CONFABULATION GUARD: a personal-recall question whose MEMORY grounding is empty (no
                # relevant memory, no graph facts, no conversation history) gets a grounding note that
                # tells Sali to CHECK the real record and never invent specifics — the failure mode was
                # him fabricating a detailed fake past from nothing. Deliberately does NOT look at
                # bundle.recent (that's the daemon's own background events, not an answer to "what did
                # we do"), and does NOT assert "no record" (which would wrongly suppress a real
                # git/file-grounded answer) — it steers toward checking, not toward denial.
                if (_is_personal_recall(user_input) and not history
                        and not bundle.memories and not bundle.graph_facts
                        and not getattr(bundle, "experiences", None)
                        and not getattr(bundle, "procedures", None)):
                    messages.append(ChatMessage(role="user", content=_RECALL_GROUNDING))
                    await journal.event("recall_grounding_empty_record", {})
                # Prompt 6 observability (§46/§47): the assembled working set + its layer sizes, against
                # the operational budget. Operational metadata only — never any prompt/tool content.
                _assembled_budget = self._budget(messages)
                await self._emit_runtime(
                    "context.assembled",
                    {"context_limit": _assembled_budget.limit, "estimated_tokens": _assembled_budget.used,
                     "usable_tokens": _assembled_budget.usable, "output_reserve": _assembled_budget.reserved_output,
                     "status": _assembled_budget.status.value, "layers": assembled.included,
                     "dropped": assembled.dropped},
                    run_id=journal.run_id,
                    task_id=(task_transition.active_task.id if task_transition.active_task else None),
                    session_id=session_id)

                # ONE STEP PER CONTINUATION TURN.
                #
                # `max_iterations` is 80 for every turn. On a task-continuation turn that is 80 model
                # calls in ONE turn — measured live at iteration 13 and still climbing three minutes in,
                # while the continuation faculty fires again every 45 seconds. Almir's words: "in that
                # way this task will be running forever!" He was right, and it also hid the progress he
                # was asking to see: a single turn grinding through several steps looks identical to a
                # stalled one, because nothing lands while it runs.
                #
                # A continuation turn's job is the NEXT step, not the whole plan. Bounding it means each
                # cycle ends with a step actually marked, the Tasks screen moves, and a stuck turn costs
                # a minute instead of twenty. The faculty picks the task straight back up, so throughput
                # is unchanged — only the granularity is.
                _is_continuation = bool(
                    task_transition.active_task is not None
                    and (user_input or "").lstrip().startswith("TASK:"))
                max_iter = (_TASK_TURN_MAX_ITER if _is_continuation
                            else self.settings.runtime.max_iterations)
                budget = self.settings.runtime.token_budget_per_run
                tokens_used = 0
                tool_calls = 0
                final_text = ""
                iteration = 0
                follow_through = 0
                last_reply = ""  # the previous tool-less reply, to catch Sali repeating itself
                json_recovery = 0
                tool_sigs: dict[str, int] = {}  # signatures of tool calls seen this turn (loop guard)
                failed_sigs = self._session_failures(session_id)  # sig -> (failures, error, when)
                repeats_at_last_nudge = 0
                ran_commands: set[str] = set()  # execute_command commands run this turn (capture guard)
                used_tools: set[str] = set()    # tool NAMES run this turn (durable-capture guard)
                # What actually happened, in order — the evidence the stall judge is shown so it can
                # weigh the reply against the record rather than against how sure the reply sounds.
                tool_record: list[tuple[str, bool]] = []
                receipt_numbers: set[int] = set()  # every number the turn's tool results returned (#7)
                _lookup_content: list[str] = []     # successful external-lookup output (Phase 4 confab)
                _LOOKUP_TOOLS = frozenset({"web_search", "web_fetch", "recall", "memory_entity"})
                _err_repeats: dict[tuple[str, str], int] = {}  # (tool, error) count — drifting-args loops
                _err_nudged = False
                # Files CREATED/modified this turn (for delivery hygiene: reference them instead of
                # letting Sali paste their whole contents into the chat — Almir's file-flood complaint).
                created_files: list[str] = []
                # --- Long-running context continuity (Prompt 3) ---
                # The LLM context is disposable; the task state is durable. These track how the working
                # prompt is folded so a task can run through MANY context windows without losing a thing.
                compaction_count = 0          # proactive + emergency folds this run
                overflow_recoveries = 0       # bounded provider-overflow recoveries this run (§8/§29)
                approaching_emitted = False   # emit "approaching limit" once per run, not every cycle
                emergency_used = False        # last-ditch capsule-only continuation used this run (§42)
                primary_task_id = (
                    task_transition.active_task.id if task_transition.active_task is not None else None
                )

                # §10 action continuity: Almir delegating execution ("run it" / "do it" / …) resolves to
                # the captured pending proposal and runs EXACTLY that — deterministically, through the same
                # policy/execute/verify pipeline — instead of letting the model reconstruct the command
                # (which is how "run it" once selected an unrelated `ss`). The model then narrates + verifies.
                if is_delegation(user_input):
                    pending_action = await pending.latest_pending(conn, session_id)
                    if pending_action is not None:
                        yield LoopEvent("status", "running what you asked")
                        yield LoopEvent("tool", pending_action.tool_name, {
                            "phase": "start", "name": pending_action.tool_name,
                            "args": redact_obj(pending_action.arguments)})
                        call = ToolCall(pending_action.tool_name, pending_action.arguments)
                        tool_msg, ok, summary = await self._handle_tool(
                            conn, journal, call, exec_id=pending_action.exec_id, internal=internal)
                        tool_calls += 1
                        if pending_action.command:
                            ran_commands.add(pending_action.command)
                        yield LoopEvent("tool", pending_action.tool_name, {
                            "phase": "done", "name": pending_action.tool_name, "ok": ok, "summary": summary})
                        await journal.event("delegated_execution",
                                            {"tool": pending_action.tool_name, "ok": ok})
                        messages.append(ChatMessage(role="user", content=(
                            f"(You proposed `{pending_action.command or pending_action.description}` and I ran "
                            "exactly that on your say-so — its real result is above. Continue the task: verify "
                            "it worked, and if this was a fix, retry whatever originally failed.)")))
                        messages.append(tool_msg)

                planned_this_turn = False
                advanced_this_turn = False
                while iteration < max_iter and tokens_used < budget:
                    # A continuation turn stops once it has actually marked a step. Anything further is
                    # the next step's work, and it belongs to the next cycle where Almir can see it land.
                    if _is_continuation and advanced_this_turn:
                        break
                    # --- TASK AUTHORITY GUARD ---
                    # Before each reasoning cycle, verify the task is still authoritative.
                    # If the task was cancelled/superseded (e.g., by a concurrent turn), stop immediately.
                    if task_transition.active_task is not None:
                        still_active = await self._task_authority.current_active()
                        if still_active is None or still_active.id != task_transition.active_task.id:
                            await journal.event("task_authority_guard", {
                                "reason": "active task changed during execution",
                                "expected": str(task_transition.active_task.id),
                                "actual": str(still_active.id) if still_active else None,
                            })
                            yield LoopEvent("status", "task changed — stopping")
                            final_text = (
                                f"Stopped: the task '{task_transition.active_task.objective}' "
                                "is no longer active."
                            )
                            break
                        # Write heartbeat to prove the task is alive (for orphan detection).
                        with contextlib.suppress(Exception):
                            await self._tasks.heartbeat(task_transition.active_task.id)
                    await journal.set_state(RunState.REASON_PLAN, iteration=iteration)
                    # Nearing the window on a long task? Fold what's done and keep going — the task
                    # never dead-ends on the context limit; it compacts and continues (§7). The decision
                    # is deterministic (provider window − reserved output), taken BEFORE the model call.
                    pressure = self._budget(messages)
                    if pressure.is_approaching and not approaching_emitted:
                        approaching_emitted = True
                        await self._emit_runtime(
                            "runtime.context_approaching_limit", pressure.as_dict(),
                            run_id=journal.run_id, task_id=primary_task_id, session_id=session_id)
                    # Brain-audit Turn 3: proactive fold DELETED. Was firing on every
                    # provider call at 50% budget - live evidence showed 6 emergency
                    # folds in 2 min collapsing 22-54k tokens to 4 messages, replacing
                    # Almir's words with an LLM paraphrase each time. The provider-overflow
                    # recovery branch further down (reason="provider_overflow") still runs
                    # if Ollama actually refuses on context, so nothing dead-ends - Almir's
                    # words just stay intact until the model itself signals overflow.
                    # Kept: pressure.is_approaching still emits
                    # runtime.context_approaching_limit above so operators can watch it.
                    if False and len(messages) > 3 and pressure.should_compact:
                        pass  # dead code path preserved for one-line rollback
                    yield LoopEvent("status", "thinking")
                    started = self.clock.now()
                    res: ChatResult | None = None
                    attempt = 0
                    while res is None:
                        try:
                            # Stream Sali's words live (like ChatGPT/Claude): the terminal commits
                            # each segment to the transcript when an action follows, so words and the
                            # ● action lines interleave in order. The transcript is append-only — we
                            # never wipe what's already shown, so Almir can read the turn top to bottom.
                            gate = _TokenGate()
                            # Brain-audit Turn 1 (REPAIRED): skip the ~10.6k-token native tools=
                            # payload ONLY on turns that provably need no tools — a greeting, thanks,
                            # or acknowledgement. The original gate skipped tools on ANY first-turn
                            # request ("iter 0 + no task + not a delegation-approval"), which describes
                            # nearly every genuine request ("search X", "list files", "turn on bluetooth")
                            # — and since tools only return at iter>0 (unreachable without a tool call at
                            # iter 0), the model was left unable to act on a fresh request and fabricated
                            # the result or printed tool-call syntax as text. Default is now SEND; a turn
                            # is stripped only when it is confidently pure-social AND none of the
                            # force-send conditions hold (a live tool call, active work, an internal
                            # self-check, or an explicit "run it" delegation always keep their tools).
                            _send_tools = (
                                self.settings.runtime.always_send_tools
                                or iteration > 0
                                or primary_task_id is not None
                                or internal
                                or is_delegation(user_input)
                                or not is_pure_social(user_input)
                            )
                            _effective_specs = specs if _send_tools else None
                            async for chunk in self.provider.chat_stream(
                                messages, tools=_effective_specs or None,
                                options=_VOICE, preset=CREATIVE
                            ):
                                if chunk.thinking:
                                    yield LoopEvent("thinking", chunk.thinking)
                                if chunk.content:
                                    # Feed the leaked-JSON gate (it still guards the final text) but do
                                    # NOT stream content live: an intermediate iteration's content is
                                    # reasoning/"let me…" narration that the write-and-erase churn came
                                    # from. Only the ONE final answer is streamed, paced, at turn end.
                                    gate.feed(chunk.content)
                                if chunk.done:
                                    res = chunk.result
                            if res is None:
                                raise ProviderError("model stream ended without a result")
                        except ProviderError as exc:
                            # EMERGENCY: the provider rejected the request as too large for the context
                            # window (§8). Durable task state is already persisted (executions, steps,
                            # checkpoints, artifacts) — so we compact HARD and retry the same logical
                            # turn, never dropping the task. Bounded (§29): after a few recoveries we
                            # stop re-sending an oversized context and surface, task still resumable.
                            if context_budget.is_context_overflow(exc):
                                if overflow_recoveries >= _MAX_OVERFLOW_RECOVERIES:
                                    # §42 EMERGENCY CONTINUATION: one last-ditch rebuild with ONLY the
                                    # minimal capsule (objective, workspace, NEXT ACTION, constraints,
                                    # reviewer failures, last execution) so Sali can still continue under
                                    # severe context pressure — never fail a resumable task outright.
                                    if not emergency_used and task_transition.active_task is not None:
                                        emergency_used = True
                                        with contextlib.suppress(Exception):
                                            cap_note = await self._task_capsule_note(
                                                task_transition.active_task, emergency=True)
                                            messages = [messages[0], ChatMessage(
                                                role="user",
                                                content=f"(EMERGENCY CONTINUATION — minimal context.\n{cap_note})")]
                                        await self._emit_runtime(
                                            "context.emergency_mode", {"reason": "overflow_unrecovered"},
                                            run_id=journal.run_id, task_id=primary_task_id,
                                            session_id=session_id)
                                        yield LoopEvent("status", "emergency continuation")
                                        continue
                                    await self._emit_runtime(
                                        "runtime.context_compaction_failed",
                                        {"reason": "overflow_unrecovered",
                                         "recoveries": overflow_recoveries, "error": str(exc)[:200]},
                                        run_id=journal.run_id, task_id=primary_task_id,
                                        session_id=session_id)
                                    raise
                                overflow_recoveries += 1
                                compaction_count += 1
                                yield LoopEvent("status", "context filled — compacting and continuing")
                                messages = await self._compact_and_continue(
                                    messages, journal, count=compaction_count,
                                    before=self._budget(messages), task_id=primary_task_id,
                                    session_id=session_id, reason="provider_overflow",
                                    user_input=user_input)
                                await self._emit_runtime(
                                    "runtime.context_overflow_recovered",
                                    {"recovery": overflow_recoveries, "kept": len(messages)},
                                    run_id=journal.run_id, task_id=primary_task_id,
                                    session_id=session_id)
                                await journal.event(
                                    "context_overflow_recovered",
                                    {"recovery": overflow_recoveries, "kept": len(messages)})
                                continue  # retry this cycle with the folded context
                            attempt += 1
                            if attempt > _MAX_PROVIDER_RETRIES:
                                raise
                            await journal.event(
                                "provider_retry", {"attempt": attempt, "error": str(exc)[:200]}
                            )
                            yield LoopEvent("status", "let me try that again")
                    latency = int((self.clock.now() - started).total_seconds() * 1000)
                    tokens_used += res.tokens_in + res.tokens_out
                    await journal.event(
                        "llm_call", {"model": res.model, "tool_calls": len(res.tool_calls)},
                        latency_ms=latency, tokens_in=res.tokens_in, tokens_out=res.tokens_out,
                    )
                    if not res.tool_calls:
                        # Content that's really a leaked tool call the parser didn't catch — a JSON
                        # blob (withheld by the gate, never shown) OR pseudocode `tool_name(...)`.
                        # Either way the tool never ran, so recover and ask for a clean redo rather
                        # than showing the raw call as an answer.
                        _tool_names = frozenset(t.name for t in self.registry.tools(available_only=False))
                        if json_recovery < _MAX_JSON_RECOVERY and _looks_like_leaked_call(
                                res.content, _tool_names):
                            json_recovery += 1
                            _leaked_json = _looks_like_leaked_tool_call(res.content)
                            messages.append(ChatMessage(role="assistant", content=res.content))
                            messages.append(ChatMessage(
                                role="user",
                                content=_JSON_RECOVERY_NUDGE if _leaked_json else _LEAKED_CALL_NUDGE))
                            await journal.event("json_recovery",
                                                {"attempt": json_recovery,
                                                 "kind": "json" if _leaked_json else "pseudocode"})
                            yield LoopEvent("status", "let me actually run that")
                            iteration += 1
                            continue
                        # Did Sali do PART of a task and then stop ("made the folder, now
                        # continuing…")? Only meaningful once real work has begun this turn, so we
                        # gate on tool_calls > 0 — a purely conversational reply (a greeting, a
                        # recall answered from memory) is taken at face value and never pays for the
                        # extra self-judge inference. Bounded so it can never spin; model judges itself.
                        # If this reply just repeats the last one, Sali is stuck spinning — take it as
                        # final instead of nudging it to say the same thing yet again.
                        repeating = _too_similar(res.content, last_reply)
                        last_reply = res.content
                        # A turn that PROMISED and called nothing is the case this judge exists for, and
                        # it was the one case it could never see: the gate was `tool_calls > 0`, and a
                        # promise-without-action is by definition a zero-tool turn. Sali told Almir "ok
                        # let me actually check tanzahost.com right now" three times, called no tool any
                        # of the three, and each reply was taken at face value and shipped.
                        # The cheap textual pre-filter keeps the original cost argument intact: an
                        # ordinary conversational reply still never pays for the extra self-judge
                        # inference, only one that made a claim about what it is about to do.
                        # Two shapes of the same lie: saying it WILL act, and saying it already HAS.
                        # Both are only suspicious when nothing actually ran this turn.
                        _said = res.content or ""
                        promised = tool_calls == 0 and (
                            bool(_PROMISE_RE.search(_said)) or bool(_CLAIMED_DONE_RE.search(_said))
                        )
                        # Coding work without a task. The instruction alone does not carry at Q2_K — it
                        # was ignored three times running — so the judge is told to look here too.
                        from sali.context.engine import _CODING_WORK_RE as _CODE_RE
                        unplanned_code = (
                            "plan_task" not in used_tools
                            and task_transition.active_task is None
                            and bool(_CODE_RE.search(user_input or ""))
                        )
                        # Worked a task and marked nothing. The continuation turn's whole purpose is to
                        # land one step; tools that ran without a mark leave the plan frozen while real
                        # work happened, which is exactly what "it shows 1 step done" looked like.
                        unmarked_step = bool(
                            _is_continuation and tool_calls > 0 and not advanced_this_turn)
                        promised = promised or unplanned_code or unmarked_step
                        # THE TASK RECORD SETTLES THIS, NOT A JUDGE.
                        #
                        # Almir: "sali must never say confirmed or done the task while the task still
                        # running … he supposed to use the task". Caught live — task 42b068e5, steps 1
                        # and 2 done, and Sali replied "Step 3 verified … Advancing step 3 to done"
                        # while step 3 was still `pending`. He had genuinely checked; he simply never
                        # called advance_task, so he announced a state change he had not made.
                        #
                        # Everything above this line hands the final word to `_stalled`, an LLM judging
                        # its own reply — and a fluent, accurate-sounding claim is exactly what such a
                        # judge accepts. Whether step 3 is done is not a matter of opinion: the row
                        # knows. So this branch is deterministic and does not consult the judge at all.
                        false_step_claim = ""
                        if (task_transition.active_task is not None
                                and (_STEP_CLAIM_RE.search(_said)
                                     or (_CLAIMED_DONE_RE.search(_said) and not advanced_this_turn))):
                            with contextlib.suppress(Exception):
                                false_step_claim = await self._task_claim_conflict(
                                    task_transition.active_task.id, _said)
                        if false_step_claim and follow_through < _MAX_FOLLOW_THROUGH:
                            follow_through += 1
                            messages.append(ChatMessage(role="assistant", content=res.content))
                            messages.append(ChatMessage(role="user", content=false_step_claim))
                            await journal.event(
                                "false_step_claim",
                                {"attempt": follow_through,
                                 "task": str(task_transition.active_task.id)})
                            yield LoopEvent("status", "let me actually land that")
                            iteration += 1
                            continue
                        if ((tool_calls > 0 or promised) and follow_through < _MAX_FOLLOW_THROUGH
                                and not repeating
                                and await self._stalled(
                                    user_input, res.content, journal,
                                    evidence=_render_tool_record(tool_record))):
                            follow_through += 1
                            messages.append(ChatMessage(role="assistant", content=res.content))
                            # Step-discipline: on a CONTINUATION turn the fallback must never be the
                            # generic "finish everything" — it must keep Sali on THIS step. Route to
                            # mark-the-step (work happened, unmarked) or finish-this-step-only, so a
                            # continuation turn can never be told to run ahead.
                            if _is_continuation:
                                _nudge = (_MARK_STEP_NUDGE if unmarked_step
                                          else _FABRICATION_NUDGE if tool_calls == 0
                                          else _STEP_INCOMPLETE_NUDGE)
                            else:
                                _nudge = (_MARK_STEP_NUDGE if unmarked_step
                                          else _PLAN_FIRST_NUDGE if unplanned_code
                                          else _FABRICATION_NUDGE if tool_calls == 0
                                          else _FOLLOW_THROUGH_NUDGE)
                            messages.append(ChatMessage(role="user", content=_nudge))
                            await journal.event("follow_through", {"attempt": follow_through})
                            yield LoopEvent("status", "on it")
                            iteration += 1
                            continue
                        # Recovery exhausted but the content is STILL a leaked tool call (JSON blob or
                        # pseudocode `tool_name(...)`) — never show Almir raw call text. Treat it as
                        # empty so the wrap-up path speaks in voice instead.
                        final_text = "" if _looks_like_leaked_call(
                            res.content,
                            frozenset(t.name for t in self.registry.tools(available_only=False))
                        ) else res.content
                        break
                    messages.append(
                        ChatMessage(role="assistant", content=res.content, tool_calls=res.tool_calls)
                    )
                    # THIS ITERATION WAS NOT THE FINAL ONE. Everything Sali streamed for it was
                    # in-progress narration ("let me check the folder"), NOT his message to Almir.
                    # The final message is always the LAST iteration - the one where the model returns
                    # with no more tool calls.
                    #
                    # Emitted at the moment we know: if `res.tool_calls` is non-empty, this iteration
                    # is intermediate. The app takes what it has streamed so far, moves it into the
                    # collapsible activity panel as a "said" entry, and clears its streaming buffer
                    # for the next iteration. Only the last iteration's text remains in the chat
                    # bubble. No extra inference; no change to total generation time.
                    #
                    # Almir on the "thinking out loud" problem: "in the iPhone chat I can see Sali
                    # effectively thinking out loud - 'let me do this / now I need to / I should check'
                    # - then if I close/reopen the app, the real final answer may appear differently."
                    # This closes that: what shows in chat is the intentional message, and the
                    # progress narration lives where it belongs - visible in activity, not the bubble.
                    yield LoopEvent("iteration_boundary", "",
                                    {"iteration": iteration, "tool_calls": len(res.tool_calls)})
                    for call in res.tool_calls:
                        tool_calls += 1
                        used_tools.add(call.name)
                        if call.name in ("advance_task", "finish_task"):
                            advanced_this_turn = True
                        # SCOPE GUARD (step-discipline): on a continuation turn, refuse a WRITE that
                        # targets a file belonging to a LATER step — the structural form of "never do
                        # anything without the step first". Redirect (don't corrupt state): skip the
                        # call, tell the model to finish the current step. Only blocks on a concrete
                        # later-step owner, so legitimate current-step writes are never impeded.
                        if (_is_continuation and task_transition.active_task is not None
                                and call.name in ("create_file", "modify_file", "delete_file")):
                            _tgt = str((call.arguments or {}).get("path") or "").strip()
                            _owner = _scope_block(task_transition.active_task, _tgt)
                            if _owner is not None:
                                _label = f"step {_owner}" if _owner > 0 else "a later step"
                                await journal.event("scope_guard_block",
                                                    {"tool": call.name, "path": _tgt[:200],
                                                     "owner_step": _owner})
                                yield LoopEvent("tool", call.name,
                                                {"phase": "done", "name": call.name, "ok": False,
                                                 "summary": f"blocked — {_label}'s work, not this step's"})
                                messages.append(ChatMessage(role="user", content=(
                                    f"(Not now — `{_tgt}` belongs to {_label}, which comes later. Do the "
                                    f"CURRENT step's work only, then mark it done; you'll be handed "
                                    f"{_label} on its own turn.)")))
                                tool_record.append((call.name, False))
                                continue
                        sig = f"{call.name}:{json.dumps(call.arguments, sort_keys=True, default=str)}"
                        yield LoopEvent("tool", call.name,
                                        {"phase": "start", "name": call.name,
                                         "args": redact_obj(call.arguments)})  # never stream a secret
                        broken = failed_sigs.get(sig)
                        if broken and broken[0] >= _MAX_TOOL_FAILURES:
                            # This exact call already failed the same way — don't run it again. Hand
                            # the failure back as a factual result so Sali changes course, not loops.
                            await journal.event("tool_circuit_break",
                                                {"tool": call.name, "failures": broken[0]})
                            tool_record.append((call.name, False))
                            yield LoopEvent("tool", call.name, {"phase": "done", "name": call.name,
                                            "ok": False, "summary": "already failed — not retried"})
                            messages.append(_circuit_broken_message(call.name, broken[0], broken[1]))
                            continue
                        tool_sigs[sig] = tool_sigs.get(sig, 0) + 1  # count executed calls only
                        # Record execution for durability (before running the tool).
                        # Use the ACTUAL current step, not a hardcoded 0 — so recovery
                        # can trace which step each tool call belongs to.
                        exec_record_id = None
                        active_step_seq: int | None = None
                        tool_for_exec = self.registry.get(call.name)
                        if task_transition.active_task is not None:
                            with contextlib.suppress(Exception):
                                active_step_seq = await self._tasks.current_step(
                                    task_transition.active_task.id)
                            with contextlib.suppress(Exception):
                                exec_record_id = await self._tasks.record_execution(
                                    task_transition.active_task.id, active_step_seq or 0, call.name,
                                    tool_args=call.arguments,
                                    idempotent=tool_for_exec.idempotent if tool_for_exec else None,
                                    attempt=tool_sigs[sig])
                        # Start a heartbeat task during tool execution so long-running tools
                        # (npm install, docker build, etc.) don't trigger orphan detection.
                        _tool_hb_task: asyncio.Task[None] | None = None
                        if task_transition.active_task is not None:
                            active_task_id = task_transition.active_task.id  # non-None for the closure
                            # Record which tool is executing (for watchdog context)
                            with contextlib.suppress(Exception):
                                await self._tasks.set_active_tool(active_task_id, call.name)
                            # Emit tool started event via canonical publisher
                            if self._publisher is not None:
                                with contextlib.suppress(Exception):
                                    from sali.events.publisher import SaliEvent
                                    await self._publisher.publish(SaliEvent(
                                        event_type="task.tool.started",
                                        task_id=active_task_id,
                                        run_id=journal.run_id,
                                        origin="tool",
                                        data={"tool": call.name, "run_id": str(journal.run_id)},
                                    ))
                            async def _heartbeat_during_tool(_tid: UUID = active_task_id) -> None:
                                while True:
                                    try:
                                        await asyncio.sleep(30)
                                        with contextlib.suppress(Exception):
                                            await self._tasks.heartbeat(_tid)
                                    except asyncio.CancelledError:
                                        break
                            _tool_hb_task = asyncio.create_task(_heartbeat_during_tool())
                        try:
                            tool_msg, ok, summary = await self._handle_tool(
                                conn, journal, call, as_subagent=as_subagent, internal=internal)
                        finally:
                            # Stop heartbeat task — always cleaned up, even on failure/cancellation
                            if _tool_hb_task is not None:
                                _tool_hb_task.cancel()
                                with contextlib.suppress(asyncio.CancelledError):
                                    await _tool_hb_task
                            # Clear active tool name
                            if task_transition.active_task is not None:
                                with contextlib.suppress(Exception):
                                    await self._tasks.set_active_tool(
                                        task_transition.active_task.id, None)
                        # Update execution record with result.
                        if exec_record_id is not None:
                            with contextlib.suppress(Exception):
                                await self._tasks.complete_execution(
                                    exec_record_id,
                                    status="completed" if ok else "failed",
                                    result_summary=summary[:200] if summary else None,
                                    error=summary[:200] if not ok else None)
                        # Record meaningful progress on tool success (not heartbeat, not prose)
                        if ok and task_transition.active_task is not None:
                            with contextlib.suppress(Exception):
                                await self._tasks.record_progress(
                                    task_transition.active_task.id, "tool_success",
                                    tool_name=call.name)
                        # Emit tool completed/failed event via canonical publisher
                        if task_transition.active_task is not None and self._publisher is not None:
                            with contextlib.suppress(Exception):
                                from sali.events.publisher import SaliEvent
                                event_type = "task.tool.completed" if ok else "task.tool.failed"
                                await self._publisher.publish(SaliEvent(
                                    event_type=event_type,
                                    task_id=task_transition.active_task.id,
                                    run_id=journal.run_id,
                                    origin="tool",
                                    data={"tool": call.name, "ok": ok,
                                          "summary": (summary or "")[:200],
                                          "run_id": str(journal.run_id)},
                                ))
                        if call.name == "execute_command" and isinstance(call.arguments.get("command"), str):
                            ran_commands.add(call.arguments["command"])  # don't re-capture what we just ran
                        # Log task-linked tool execution to sali-works (filesystem backup).
                        if task_transition.active_task is not None:
                            with contextlib.suppress(Exception):
                                append_event(
                                    task_transition.active_task.id, "tool_executed",
                                    {"tool": call.name, "ok": ok,
                                     "summary": (summary or "")[:200],
                                     "step_seq": active_step_seq})
                        # A clean, persistent progress line: what was done + a short result.
                        tool_record.append((call.name, bool(ok)))
                        if ok:  # capture the real numbers this result returned, for the #7 quantity check
                            with contextlib.suppress(Exception):
                                from sali.verify.response_claims import _numbers_in
                                receipt_numbers |= _numbers_in(getattr(tool_msg, "content", "") or "")
                                receipt_numbers |= _numbers_in(summary or "")
                            if call.name in _LOOKUP_TOOLS:  # external-lookup output (Phase 4 confab check)
                                _lookup_content.append(getattr(tool_msg, "content", "") or "")
                        if ok and call.name in ("create_file", "modify_file"):
                            _cp = (call.arguments or {}).get("path")
                            if _cp:
                                created_files.append(str(_cp))
                        yield LoopEvent("tool", call.name, {"phase": "done", "name": call.name,
                                                            "ok": ok, "summary": summary})
                        messages.append(tool_msg)
                        if ok and call.name == "plan_task" and not as_subagent:
                            planned_this_turn = True
                        if ok:
                            # It works now — whatever was wrong has been fixed, so stop refusing it.
                            failed_sigs.pop(sig, None)
                        else:  # remember the failure (redacted — the summary is raw error text)
                            prev = failed_sigs.get(sig, (0, "", 0.0))[0]
                            failed_sigs[sig] = (prev + 1, str(redact_obj(summary)),
                                                time.monotonic())
                            # Also count by (tool, error) regardless of ARGS: a Q2_K model rephrases a
                            # broken command each retry, so every attempt gets a fresh sig and the exact
                            # breaker never fires — but the same specific error recurring is the real loop.
                            _enorm = " ".join(str(redact_obj(summary) or "").split())[:80]
                            if _enorm:
                                _ek = (call.name, _enorm)
                                _err_repeats[_ek] = _err_repeats.get(_ek, 0) + 1
                    # Going in circles? Break the investigate-forever loop with a course-correction.
                    # HAND OFF planned work instead of doing it inline. Asking the model nicely in the
                    # tool's output was not enough — it planned a 4-step task and then carried straight on
                    # executing it in the same foreground turn, holding the one cognition slot and leaving
                    # Almir's chat blocked behind a spinner for many minutes. Steering with a nudge is the
                    # mechanism this loop already uses (see _ANTI_LOOP_NUDGE below), so the turn ends with
                    # a reply and the daemon's task-continuation faculty does the work.
                    if planned_this_turn and not _is_continuation:
                        # STRUCTURAL handoff — not a prompt the model can ignore (it did: it planned,
                        # then built all six files inline in one foreground turn, every step still
                        # 'pending', which is the "steps 2 and 3 while on step 1" anti-pattern
                        # front-loaded into a single turn). A foreground turn that just planned a task
                        # is DONE: end it here with a brief voiced reply, and the continuation driver
                        # works the task ONE step at a time where Almir can watch. Breaking the loop is
                        # what makes it impossible to keep building inline.
                        planned_this_turn = False
                        await journal.event("task_handoff", {})
                        yield LoopEvent("status", "planned it — I'll work it step by step")
                        messages.append(ChatMessage(role="user", content=_HANDOFF_NUDGE))
                        acc = ""
                        async for ev in self._voice_stream(messages, journal):
                            if ev.kind == "_final":
                                acc = ev.text
                            else:
                                yield ev
                        final_text = acc.strip() or (
                            "On it — I've planned this out and I'll work through it step by step. "
                            "I'll tell you when it's done.")
                        break
                    repeats = sum(count - 1 for count in tool_sigs.values() if count > 1)
                    if repeats - repeats_at_last_nudge >= _LOOP_REPEATS:
                        repeats_at_last_nudge = repeats
                        messages.append(ChatMessage(role="user", content=_ANTI_LOOP_NUDGE))
                        await journal.event("loop_break", {"repeats": repeats})
                    # Same tool failing with the same error 3x on DRIFTING args — the exact breaker can't
                    # see it. Soft-nudge ONCE per turn (never a hard block, so it can't wrongly stop a
                    # legitimately-distinct call): course-correct or tell Almir you're stuck.
                    elif not _err_nudged and any(v >= _LOOP_REPEATS for v in _err_repeats.values()):
                        _err_nudged = True
                        messages.append(ChatMessage(role="user", content=_ANTI_LOOP_NUDGE))
                        await journal.event("loop_break",
                                            {"same_error_repeats": max(_err_repeats.values())})
                        yield LoopEvent("status", "getting unstuck")
                    iteration += 1
                else:
                    if not final_text:  # ran long — let Sali wrap up in its own voice, streamed
                        yield LoopEvent("status", "wrapping up")
                        messages.append(ChatMessage(
                            role="user",
                            content="(You've done plenty here — wrap up now in your own words, no more tools.)",
                        ))
                        acc = ""
                        async for ev in self._voice_stream(messages, journal):
                            if ev.kind == "_final":
                                acc = ev.text
                            else:
                                yield ev
                        final_text = acc.strip() or "Let me stop here for now."

                if not final_text.strip():
                    # The model broke out of the loop with an empty answer (typically after exhausting
                    # failing tool attempts). Never record silence — have Sali explain what happened,
                    # in its own voice, streamed; a plain fallback if even that comes back empty.
                    yield LoopEvent("status", "wrapping up")
                    messages.append(ChatMessage(role="user", content=_EMPTY_WRAP_NUDGE))
                    acc = ""
                    async for ev in self._voice_stream(messages, journal):
                        if ev.kind == "_final":
                            acc = ev.text
                        else:
                            yield ev
                    final_text = acc.strip() or _EMPTY_FALLBACK

                # RESPONSE-CLAIM GROUNDING (§10/§11/§41/§52) — the keystone reliability pass. Before the
                # reply is persisted OR streamed, adjudicate Sali's OWN operational claims against this
                # turn's receipts + the live machine, and strike any he cannot back: a past-tense action
                # with no effectful tool this turn, a state the machine disproves, a capability this
                # runtime has no tool for. Deterministic (no model call) and PRECISION-FIRST — it only
                # rewrites a claim it can prove false, so a true statement is never touched. This is the
                # guarantee that a fabricated "done"/"I found the owner"/"I restarted it"/"I can call your
                # phone" never reaches Almir: every tanzahost lie in the transcript ran zero tools and is
                # exactly what this catches. Fail-open on wording (the reviewer + follow-through remain).
                with contextlib.suppress(Exception):
                    from sali.verify.response_claims import validate_response
                    _cap_of = {t.name: t.capabilities
                               for t in self.registry.tools(available_only=False)}
                    _rv = await validate_response(final_text, receipts=tool_record, cap_of=_cap_of,
                                                  receipt_numbers=receipt_numbers)
                    if _rv.changed:
                        await journal.event("response_claim_struck", {
                            "kinds": sorted({c.kind for c in _rv.struck}),
                            "count": len(_rv.struck),
                            "details": [c.detail for c in _rv.struck][:5]})
                        with contextlib.suppress(Exception):
                            await self._emit(conn, "grounding.response_corrected", session_id, {
                                "run_id": str(journal.run_id),
                                "kinds": sorted({c.kind for c in _rv.struck}),
                                "count": len(_rv.struck)}, subject_type="conversation")
                        # DURABLE self-claim ledger (§32/§44/§45): the strike is REMEMBERED, not just
                        # corrected in place — one row per struck claim — so /grounding can say how often
                        # Sali over-claims and in which family, and a human can read back the exact
                        # sentences that were struck. This is the measurement the audit asks for: a
                        # self-caught contradiction is Sali's own limit, recorded so it can be learned.
                        with contextlib.suppress(Exception):
                            from sali.runtime.grounding_log import GroundingLog
                            await GroundingLog(self.pool).record(
                                session_id=session_id, run_id=journal.run_id, claims=_rv.struck)
                        final_text = _rv.rewritten

                # CROSS-TURN CONSISTENCY (§ Phase 3 #5, FLAG-ONLY): a blatant existence flip — Sali
                # negating this turn what an earlier reply asserted (the /about-us.html "it's at X" then
                # "404" pattern). It NEVER rewrites the reply; it records a conflict (an event + a
                # 'cross_turn'/'flagged' ledger row the over-claim learner ignores) so it can be measured
                # before it is ever allowed to change wording. Fail-open.
                with contextlib.suppress(Exception):
                    from types import SimpleNamespace as _NS

                    from sali.verify.consistency import find_conflicts
                    _priors = [r["content"] for r in await conn.fetch(
                        "SELECT content FROM message WHERE conversation_id=$1 AND role='assistant' "
                        "ORDER BY seq DESC LIMIT 8", session_id)]
                    _xt = find_conflicts(final_text, _priors)
                    if _xt:
                        await self._emit(conn, "grounding.cross_turn_conflict", session_id, {
                            "run_id": str(journal.run_id), "count": len(_xt),
                            "conflicts": _xt[:3]}, subject_type="conversation")
                        from sali.runtime.grounding_log import GroundingLog as _GL
                        await _GL(self.pool).record(
                            session_id=session_id, run_id=journal.run_id,
                            claims=[_NS(kind="cross_turn", verdict="flagged", sentence=c["now"],
                                        detail={"token": c["token"], "earlier": c["earlier"]})
                                    for c in _xt])

                # CONFABULATION (§ Phase 4, FLAG-ONLY): after a lookup that RAN yet returned nothing, Sali
                # sometimes NAMES a specific entity as the answer (the invented "Alfred Mrema" owner). Flag
                # it (event + a 'confabulation'/'flagged' ledger row the learner ignores) — never rewrite.
                with contextlib.suppress(Exception):
                    if any(n in _LOOKUP_TOOLS for n, _ in tool_record):
                        from types import SimpleNamespace as _NSC
                        from sali.verify.confabulation import find_confabulations
                        _cf = find_confabulations(final_text, " ".join(_lookup_content), lookup_ran=True)
                        if _cf:
                            await self._emit(conn, "grounding.confabulation_flagged", session_id, {
                                "run_id": str(journal.run_id), "count": len(_cf),
                                "entities": [c["entity"] for c in _cf][:5]}, subject_type="conversation")
                            from sali.runtime.grounding_log import GroundingLog as _GLC
                            await _GLC(self.pool).record(
                                session_id=session_id, run_id=journal.run_id,
                                claims=[_NSC(kind="confabulation", verdict="flagged",
                                             sentence=c["sentence"], detail={"entity": c["entity"]})
                                        for c in _cf])

                # DATA FABRICATION (§ Phase 4 marquee, FLAG-ONLY): a reply presenting live-system data (a
                # file/process table, command output, "here are the files:" listing) when NO observation
                # tool ran this turn — the invented file-listing shape. Flag (event + 'data_output'/
                # 'flagged' ledger row the learner ignores), never rewrite; measured before any strike.
                with contextlib.suppress(Exception):
                    from types import SimpleNamespace as _NSD
                    from sali.verify.data_output import find_data_fabrication
                    _capof2 = {t.name: t.capabilities for t in self.registry.tools(available_only=False)}
                    _df = find_data_fabrication(final_text, tool_record, _capof2)
                    if _df:
                        await self._emit(conn, "grounding.data_fabrication_flagged", session_id, {
                            "run_id": str(journal.run_id), "count": len(_df),
                            "framing": [d["framing"] for d in _df][:3]}, subject_type="conversation")
                        from sali.runtime.grounding_log import GroundingLog as _GLD
                        await _GLD(self.pool).record(
                            session_id=session_id, run_id=journal.run_id,
                            claims=[_NSD(kind="data_output", verdict="flagged",
                                         sentence=d["sentence"], detail={"framing": d["framing"]})
                                    for d in _df])

                # A file Sali sent/produced this turn is attached to THIS reply message (one durable
                # message, not a separate bubble). Filled by the research + send blocks below.
                _reply_attachment: dict[str, Any] | None = None

                # DELIVERY HYGIENE (Almir §): the chat carries a SUMMARY + references, never raw dumps —
                # the full detail goes to a downloadable file. (a) A build turn that pasted whole file
                # contents is reduced to "created X, Y" while the files stay on disk + downloadable, so a
                # big project can't bury the chat. (b) A research turn's findings are saved as a full
                # downloadable report and the chat shows a short summary + a note it can be downloaded.
                # Fail-open — a completed reply is never broken by this.
                with contextlib.suppress(Exception):
                    from pathlib import Path as _Path
                    from sali.runtime import delivery as _delivery
                    from sali.runtime.research_intent import (
                        is_research_request as _is_research_req, wants_depth as _wants_depth)
                    # EXPLICIT research → a downloadable report + a short chat summary (Almir: "not
                    # everything is a research until i say so"; a normal question is plain chat, no file).
                    if _is_research_req(user_input):
                        from sali.runtime.reports import ResearchReportStore
                        # Sali may write his findings INLINE (final_text) or to a FILE via create_file
                        # (then his reply is a terse "saved to <local path>" Almir can't reach). Either
                        # way, build OUR downloadable report from the real content and SEND it, replacing
                        # the chat with a summary. Only sectioned-expand the inline case — a file Sali
                        # already wrote is the full report.
                        _overview = final_text
                        _from_file = False
                        if created_files:
                            _best = ""
                            for _f in created_files:
                                with contextlib.suppress(Exception):
                                    _txt = _Path(_f).read_text(encoding="utf-8", errors="ignore")
                                    if len(_txt) > len(_best):
                                        _best = _txt
                            if len(_best) > len(_overview):
                                _overview, _from_file = _best, True
                        if len(_overview.strip()) >= _RESEARCH_REPORT_MIN_CHARS:
                            _full = _overview
                            _sections = 0
                            if (not _from_file and (len(_overview) >= _RESEARCH_SECTION_TRIGGER_CHARS
                                                    or _wants_depth(user_input))):
                                _titles = await self._research_outline(
                                    user_input, _overview, _MAX_RESEARCH_SECTIONS)
                                if _titles:
                                    _parts = [_overview]
                                    _t0 = self.clock.now()
                                    for _i, _title in enumerate(_titles, 1):
                                        if (self.clock.now() - _t0).total_seconds() > _RESEARCH_TIME_BUDGET_S:
                                            break  # wall-clock bound — stays minutes, never hours
                                        yield LoopEvent(
                                            "status", f"writing the report — section {_i}/{len(_titles)}")
                                        _sec = await self._research_section(user_input, _title)
                                        if _sec:
                                            _parts.append(f"## {_title}\n\n{_sec}")
                                            _sections += 1
                                    _full = "\n\n".join(_parts)
                            _summary = None
                            with contextlib.suppress(Exception):
                                _summary = await self._summarise_for_chat(_overview)
                            _summary = (_summary or _delivery.extractive_lead(_overview)).strip()
                            _title = (user_input or "Research").strip().splitlines()[0][:80]
                            _report = await ResearchReportStore(self.pool).create(
                                session_id=session_id, run_id=journal.run_id, title=_title,
                                query=user_input, full_text=_full, summary=_summary,
                                root=str(self.settings.permissions.workspace))
                            with contextlib.suppress(Exception):
                                # Emit the event the app ALREADY renders as a downloadable file card in
                                # the chat (task.artifact.created — "Sali must be able to send files
                                # back"), so the report shows up like any other file Almir can download,
                                # no app rebuild needed. artifact_id/filename/size/download_url are the
                                # exact keys the client reads.
                                await self._emit(conn, "task.artifact.created", session_id, {
                                    "run_id": str(journal.run_id), "artifact_id": _report["id"],
                                    "filename": _report["filename"], "size": _report["bytes"],
                                    "download_url": _report["download_url"], "artifact_type": "report",
                                }, subject_type="conversation")
                            _reply_attachment = {
                                "artifact_id": _report["id"], "filename": _report["filename"],
                                "size": _report["bytes"], "download_url": _report["download_url"],
                                "kind": "report"}
                            _extra = f", {_sections} sections" if _sections else ""
                            final_text = (_summary + f"\n\n📄 I've put the full write-up ({_report['words']} "
                                          f"words{_extra}) in a report — sending you the file now.")
                    elif created_files:
                        # A BUILD turn that pasted whole file contents — strip them, reference the files.
                        _clean, _stripped = _delivery.strip_file_dumps(final_text, created_files)
                        if _stripped:
                            final_text = _clean
                            await journal.event("chat_file_dump_stripped", {"files": _stripped[:12]})

                # Files Sali SENT this turn via send_file → surface each as a downloadable file card in
                # the chat using the event the app ALREADY renders (task.artifact.created). Almir is on
                # his phone; a local path never reaches him — this is how the file actually gets to him,
                # the SAME way task files show with a download option ("use the same way").
                with contextlib.suppress(Exception):
                    from pathlib import Path as _P
                    from sali.runtime import delivery as _delivery3
                    from sali.runtime.sent_files import SentFileStore
                    # DETERMINISTIC SEND: the model is unreliable at calling send_file (it said "sent"
                    # without sending). If Almir CLEARLY asked to send a specific file/folder and it
                    # wasn't already sent this turn, the runtime sends it ITSELF — so "send me X" always
                    # delivers, not model-dependent.
                    with contextlib.suppress(Exception):
                        _existing = {s["filename"]
                                     for s in await SentFileStore(self.pool).for_run(journal.run_id)}
                        _ws = str(self.settings.permissions.workspace)
                        _searchdirs = [_ws, str(_P(_ws).parent),
                                       str(_P.home() / "Desktop"), str(_P.home())]
                        _tgt = _delivery3.send_request_target(user_input, _searchdirs)
                        # "send me that / it / the file you made" → the LAST file discussed: made this
                        # turn, then named in a recent message, then most-recently-touched in the workspace.
                        if _tgt is None and _delivery3.is_vague_send(user_input):
                            _cand = created_files[-1] if created_files else None
                            if _cand is None:
                                with contextlib.suppress(Exception):
                                    _recent = await conn.fetch(
                                        "SELECT content FROM message WHERE conversation_id = $1 "
                                        "ORDER BY seq DESC LIMIT 12", session_id)
                                    for _r in _recent:
                                        _f = _delivery3.file_ref_in_text(_r["content"] or "", _searchdirs)
                                        if _f:
                                            _cand = _f
                                            break
                            if _cand is None:
                                with contextlib.suppress(Exception):
                                    _fs = [f for f in _P(_ws).iterdir() if f.is_file()]
                                    if _fs:
                                        _cand = str(max(_fs, key=lambda f: f.stat().st_mtime))
                            if _cand and _P(_cand).exists():
                                _tgt = (_cand, _P(_cand).is_dir())
                        if _tgt is not None:
                            _tpath, _tzip = _tgt
                            _stem = _P(_tpath).stem
                            if not any(_stem in n for n in _existing):
                                await SentFileStore(self.pool).register(
                                    session_id=session_id, run_id=journal.run_id, source_path=_tpath,
                                    root=_ws, zip_it=_tzip)
                    # Link this run's sent files to the CONVERSATION so GET /conversation can return them
                    # and they survive a reload (durable delivery — a live-only card vanished on refresh).
                    with contextlib.suppress(Exception):
                        await conn.execute(
                            "UPDATE sali.sent_file SET session_id = $1 "
                            "WHERE run_id = $2 AND session_id IS NULL", session_id, journal.run_id)
                    _seen_names: set[str] = set()   # dedup: the model often calls send_file twice
                    for _sf in await SentFileStore(self.pool).for_run(journal.run_id):
                        if _sf["filename"] in _seen_names:
                            continue
                        _seen_names.add(_sf["filename"])
                        if _reply_attachment is None:   # carry the file ON this reply (one message)
                            _reply_attachment = {
                                "artifact_id": _sf["id"], "filename": _sf["filename"],
                                "size": _sf["bytes"], "download_url": _sf["download_url"],
                                "kind": _sf.get("kind", "file")}
                        await self._emit(conn, "task.artifact.created", session_id, {
                            "run_id": str(journal.run_id), "artifact_id": _sf["id"],
                            "filename": _sf["filename"], "size": _sf["bytes"],
                            "download_url": _sf["download_url"],
                            "artifact_type": _sf.get("kind", "file")}, subject_type="conversation")
                    # A file WAS sent this turn (by the model OR the deterministic send) — make the reply
                    # say so cleanly, overriding any "I didn't send it" the grounding wrote when the model
                    # failed to call the tool but we sent it anyway.
                    if _reply_attachment is not None:
                        _fn = _reply_attachment["filename"]
                        _lc = final_text.lower()
                        # Keep the model's reply ONLY if it already clearly acknowledges THIS send
                        # (names the file + says sent/download/chat); otherwise it doesn't match what was
                        # actually delivered (e.g. "you didn't specify which one" while we sent it), so
                        # override to the plain truth.
                        _ack = (_P(_fn).stem[:16].lower() in _lc
                                and ("sent" in _lc or "download" in _lc or "in your chat" in _lc))
                        if not _ack:
                            final_text = f"Sent — **{_fn}** is in your chat below, tap to download."

                # Almir is on his phone — an absolute machine path in the reply reads as "Sali sent me a
                # path, not a file". Strip local paths to their filename (the file itself arrives as a
                # card). Only when he is NOT sitting at the local terminal (where a path IS useful) — the
                # connection self-model (§17-28) says which; default to stripping when unknown/remote.
                with contextlib.suppress(Exception):
                    _conn = getattr(self, "_connection", None)
                    _strip_paths = (_conn is None
                                    or getattr(_conn, "on_a_phone", False)
                                    or getattr(_conn, "is_remote", False))
                    if _strip_paths:
                        from sali.runtime import delivery as _delivery2
                        final_text = _delivery2.strip_local_paths(final_text)

                # §10: if this turn PROPOSED a runnable command but didn't run it, capture it as a durable
                # pending action so a later "run it"/"do it" resolves to exactly this — never a reconstruction.
                with contextlib.suppress(Exception):  # capture is a nicety, never break a completed turn
                    proposal = extract_proposed_command(final_text)
                    if proposal is not None and proposal["command"] not in ran_commands:
                        exec_tool = self.registry.get("execute_command")
                        risk = int(exec_tool.assess({"command": proposal["command"]})) if exec_tool else 0
                        await pending.capture(
                            conn, journal.run_id, "execute_command", {"command": proposal["command"]},
                            description=proposal["description"], risk_level=risk)

                await journal.set_state(RunState.LEARN)
                async with self.pool.acquire() as learn_conn:
                    await observe(learn_conn, kind="turn", content=user_input,
                                  source=MemorySource.CONVERSATION, session_id=session_id)
                # GUARANTEED DURABLE CAPTURE.
                #
                # Asking the model to call `remember` works about half the time on a Q2_K model, and the
                # failures are the damaging kind. Observed on 2026-09-03: "actually I use Neovim now, not
                # Helix" produced a memory_forget and NO remember — the old preference was retired, the
                # new one was never written, and Sali replied "Updated — your preferred editor is now
                # Neovim", which was false. A reworded nudge then produced no tool call at all.
                #
                # So capture does not depend on the model choosing a tool. When Almir says something meant
                # to outlive the turn and no `remember` happened, a dedicated structured extraction runs
                # here, after the reply is already on its way to him, and writes the memory itself. The
                # model is still doing the understanding — deciding what the fact is and what topic it
                # belongs to — but the decision to persist is the runtime's, not the model's.
                if "remember" not in used_tools and _durable_signal(user_input, final_text):
                    with contextlib.suppress(Exception):  # capture is a nicety, never break a done turn
                        await self._capture_durable(user_input, final_text, journal)
                # A durable OBJECTIVE (not a request) becomes a first-class goal that will feed
                # the agenda synthesiser on every future turn. Gated conservatively (see
                # _goal_signal) and idempotent (see _capture_goal), so noise doesn\'t fill the
                # goal table - a false positive on the gate just costs one extra model call.
                with contextlib.suppress(Exception):
                    await self._capture_goal(user_input, journal)
                # A PROMISE WITH A DEADLINE BECOMES A COMMITMENT HE CAN BE HELD TO.
                with contextlib.suppress(Exception):
                    await self._capture_promise(final_text, task_id=None, journal=journal)
                # A follow-up WITHOUT a deadline ("I'll look into that") becomes an OPEN LOOP —
                # the "still-on-my-mind" register the InitiativeEngine consults each cycle.
                # Cheap; deduped by LOWER(title); TTL 14d so dropped loops age out.
                with contextlib.suppress(Exception):
                    await self._capture_open_loop(final_text, task_id=None, journal=journal)
                await self._append_message(
                    conn, session_id, "assistant", final_text, model=self.settings.model.chat_model,
                    attachment=_reply_attachment,
                )
                await journal.set_state(RunState.RESPOND)
                await journal.event("respond", {"chars": len(final_text)})
                await journal.set_state(RunState.DONE)
                await journal.finish("completed")
                await self._emit(
                    conn, "conversation.turn", session_id,
                    {"run_id": str(journal.run_id), "tools": tool_calls}, subject_type="conversation",
                )
                # STREAM THE FINAL ANSWER, ONE PIECE AT A TIME. Content was never streamed live during
                # the turn (that was the reasoning/write-erase churn Almir called out); instead the ONE
                # clean final answer is typed out here as paced 'token' deltas, so the app renders it
                # live word-by-word — "streaming writing one by one the output" — with zero reasoning
                # ever shown. The durable agent.final below still carries the whole text for reconnect
                # replay and as the authoritative settle.
                for _piece in _pace_chunks(final_text):
                    yield LoopEvent("token", _piece)
                    await asyncio.sleep(0.025)
                yield LoopEvent("final", final_text, {
                    "run_id": str(journal.run_id), "iterations": iteration, "tool_calls": tool_calls,
                })
                # Post-completion housekeeping — the run is already DONE, so a hiccup here must
                # NOT flip a finished turn to FAILED. Ack the machine-changes we surfaced (only
                # now that the turn actually reached the model) and fold the session if long.
                try:
                    if ack_changes_through is not None and _mc_reached_model:
                        await twin_awareness.acknowledge(conn, ack_changes_through)
                    if ack_obs_through is not None and _mc_reached_model:
                        await twin_awareness.acknowledge_observations(conn, ack_obs_through)
                    await self._self_state.record_outcome(success=True, summary=user_input)
                    # COMPACTION IS DEFERRED, NOT INLINE.
                    #
                    # `_maybe_compact` calls the same 35B model as a user turn and enters the same
                    # `_inference_lease` — so a synchronous compact between turns holds the lease the
                    # NEXT turn needs. Measured on live data before this change: n=14 folds,
                    # p50=11.4 s, p90=19.7 s, max=20.4 s — every follow-up on a ~1-in-17 turn saw that
                    # entire block added to its TTFT.
                    #
                    # Scheduled instead: the task holds the same lease when it actually runs, so
                    # one-runtime is preserved. When Almir speaks again, a rapid follow-up cancels an
                    # in-flight compaction so the second turn does not eat the stall either;
                    # compaction is incremental (via summary_through_seq), so the next idle window
                    # simply picks up where it left off.
                    self._schedule_compact(session_id)
                    await prune_stm(conn)  # reclaim expired short-term rows every turn (cheap, no model)
                    self._maybe_learn()  # fold raw activity into episodes/procedures — off-thread
                except Exception as exc:  # noqa: BLE001 - housekeeping, never fail a done turn
                    self.log.warning("post_turn_housekeeping_failed", error=str(exc))
            except asyncio.CancelledError:
                # Interrupted (Ctrl-C, WebSocket disconnect, shutdown). Don't leave the run stuck as
                # 'running' with an orphaned 'executing' tool row — mark both aborted, then let the
                # cancellation propagate. Best-effort: suppress ordinary errors, never swallow the
                # cancellation itself.
                with contextlib.suppress(Exception):
                    await conn.execute(
                        "UPDATE tool_execution SET status='aborted', finished_at=now() "
                        "WHERE run_id=$1 AND status='executing'",
                        journal.run_id,
                    )
                    await journal.event("run.interrupted", {})
                    await journal.set_state(RunState.ABORTED)
                    await journal.finish("aborted")
                raise
            except Exception as exc:
                self.log.error("run_failed", run_id=str(journal.run_id), error=str(exc))
                await journal.event("run.error", {"error": str(exc)})
                await journal.set_state(RunState.FAILED)
                await journal.finish("failed")
                with contextlib.suppress(Exception):
                    await self._self_state.record_outcome(success=False, summary=str(exc)[:200])
                raise

    async def _handle_tool(
        self, conn: Any, journal: RunJournal, call: ToolCall, *, exec_id: UUID | None = None,
        as_subagent: bool = False, internal: bool = False,
    ) -> tuple[ChatMessage, bool, str]:
        """Run one tool; return (message-for-the-model, succeeded?, short summary) — the flag +
        summary let the UI print a clean ✓/✗ progress line of what was actually done."""
        tool = self.registry.get(call.name)
        if tool is None:
            await journal.event("tool.unknown", {"name": call.name})
            return _tool_message(call.name, {"error": f"unknown tool '{call.name}'"}), False, "unknown tool"

        # VERBATIM ANCHOR: correct a garbled rare token in the tool's ARGUMENTS back to what Almir
        # actually typed (or the canonical workspace name) BEFORE the tool runs — the 2-bit model
        # retyped "tanzahost" as "tanzhost" in 17 calls, sending whois/curl/web_search to the wrong
        # site. Precision-first + fail-open; every correction is journaled so it is never silent.
        with contextlib.suppress(Exception):
            from sali.runtime.verbatim import anchor_args
            _fixed_args, _corr = anchor_args(
                call.arguments, getattr(self, "_turn_anchors", None),
                workspace=getattr(self, "_workspace_name", "sali-works"))
            if _corr:
                call.arguments = _fixed_args
                await journal.event("tool.arg_corrected",
                                    {"tool": tool.name, "corrections": _corr[:8]})

        decision = self.policy.decide(tool, call.arguments)
        decision = await self._authority_floor(tool, call.arguments, decision)
        await journal.event(
            "policy",
            {"tool": tool.name, "action": decision.action.value, "risk": int(decision.risk)},
        )

        # SHADOW/ADVISORY judgment (§5/§6): run the dormant capability–authority–consequence layer on
        # effectful tools and JOURNAL its read (capability vs authority vs consequence as distinct axes —
        # richer than the risk-only policy gate that actually decides). ADVISORY ONLY: the tool exists so
        # capability is available and this can never block; it changes nothing, only records. Effectful
        # tools only (a read loop stays untouched), fail-open.
        with contextlib.suppress(Exception):
            from sali.core.enums import Capability as _Cap
            _caps = getattr(tool, "capabilities", frozenset())
            if _caps & {_Cap.WRITE, _Cap.EXECUTE, _Cap.DESTRUCTIVE, _Cap.SYSTEM, _Cap.NETWORK}:
                from sali.runtime.judgment import CapabilityAction as _CA
                from sali.runtime.judgment import assess as _assess
                _jd = _assess(_CA(
                    capability=sorted(c.value for c in _caps)[0],
                    action=tool.name,
                    target=(str(call.arguments.get("path") or call.arguments.get("url")
                                or call.arguments.get("command") or call.arguments.get("target") or "")
                            [:80] or None),
                    scope=("machine" if _Cap.SYSTEM in _caps else
                           "external" if _Cap.NETWORK in _caps else "local"),
                    reversible=_Cap.DESTRUCTIVE not in _caps,
                    external=_Cap.NETWORK in _caps,
                    destructive=_Cap.DESTRUCTIVE in _caps,
                ), capability_available=True)
                await journal.event("judgment", {"tool": tool.name, "level": _jd.level.value,
                                                 "reasons": _jd.reasons[:4]})

        if decision.action is Action.DENY:
            await journal.set_state(RunState.TOOL_DENIED)
            await self._audit(conn, journal.run_id, tool, call.arguments, decision, None, None, None)
            await self._emit(conn, "tool.denied", journal.run_id,
                             {"tool": tool.name, "reason": decision.reason})
            return _tool_message(tool.name, {"denied": decision.reason}), False, "blocked by policy"

        if decision.action is Action.CONFIRM:
            await journal.set_state(RunState.AWAIT_CONFIRM)
            if not await self.confirmer.confirm(tool, call.arguments, decision):
                await journal.set_state(RunState.TOOL_DENIED)
                await self._audit(
                    conn, journal.run_id, tool, call.arguments, decision, None, None, None
                )
                await self._emit(conn, "tool.denied", journal.run_id,
                                 {"tool": tool.name, "reason": "user declined"})
                return _tool_message(tool.name, {"denied": "user declined"}), False, "you declined"

        # Write-ahead: the 'executing' row is committed BEFORE the side effect (fix M15). A delegated
        # pending action passes its pre-recorded 'planned' row's id, so it transitions planned →
        # executing → verified_* on the SAME row (§6 honesty) instead of leaving an orphan.
        plan_json = {"args": redact_obj(call.arguments)}
        if exec_id is None:
            exec_id = new_id()
            await conn.execute(
                "INSERT INTO tool_execution "
                "  (id, run_id, run_kind, tool_name, status, danger_level, approved_by, plan) "
                "VALUES ($1,$2,'agent_run',$3,'executing',$4,$5,$6)",
                exec_id, journal.run_id, tool.name, int(decision.risk),
                f"policy:{decision.action.value}", plan_json,
            )
        else:
            await conn.execute(
                "UPDATE tool_execution SET status='executing', danger_level=$2, approved_by=$3, "
                "  plan=$4, started_at=now() WHERE id=$1",
                exec_id, int(decision.risk), f"policy:{decision.action.value}", plan_json,
            )
        await journal.set_state(RunState.EXECUTE_TOOL)
        started = self.clock.now()
        # Resolve workspace from the active task (if any).
        workspace = None
        try:
            active_task = await self._task_authority.current_active()
            if active_task and active_task.workspace_root:
                from sali.tasks.workspace import TaskWorkspace
                workspace = TaskWorkspace.from_dict({
                    "workspace_root": active_task.workspace_root,
                    "allowed_write_roots": active_task.allowed_write_roots or [active_task.workspace_root],
                })
        except Exception:  # noqa: BLE001 - workspace is best-effort
            pass

        ctx = ToolContext(settings=self.settings, clock=self.clock, pool=self.pool,
                          run_id=journal.run_id,
                          memory=self._memory_sink, recall=self._recall, graph=self._graph,
                          tasks=self._tasks, task_authority=self._task_authority,
                          reviewer=self._reviewer,
                          research=_ResearchSink(store=self._research_store, tasks=self._tasks,
                                                 run_id=journal.run_id, pool=self.pool),
                          decisions=self._decisions, phases=self._phases,
                          clarify=_ClarifySink(store=self._questions, tasks=self._tasks,
                                               run_id=journal.run_id),
                          is_subagent=as_subagent, internal=internal,
                          workspace=workspace,
                          schedules=self._schedules, documents=self._documents, remote=self._remote,
                          comms=self._comms, browser=self._browser, vision=self._vision,
                          perception=self._perception, catalog=self._catalog,
                          self_model=self._self_sink, health=self._health)
        result = await dispatch.run_tool(tool, call.arguments, ctx)

        await journal.set_state(RunState.OBSERVE)
        await journal.set_state(RunState.VERIFY)
        try:
            verify = await tool.verify(call.arguments, result, ctx)
        except Exception as exc:  # noqa: BLE001 - a raising verifier is a failed verification,
            verify = VerifyResult(False, f"verify raised: {exc}")  # not an orphaned executing row
        duration = int((self.clock.now() - started).total_seconds() * 1000)
        success = result.ok and verify.success

        # Redact the whole observed payload — every effectful tool's output/stderr flows through
        # here into the durable record AND back to the model (fix: asymmetric redaction).
        # THE `error` COLUMN, which nothing has ever written. Measured on the live database: 217
        # executions, 24 of them failures, `count(error) = 0`. The text was going only into the
        # `observed` jsonb, while two consumers read the dedicated column and therefore saw nothing —
        # world_state.py:110 guards on `if r["success"] is False and r["error"]`, so the "Recent
        # errors" block Sali is shown every relevant turn has been structurally empty since it was
        # written. He could see THAT something failed and never WHY, which is the first question §21
        # asks him to answer before retrying.
        error_text: str | None = None
        if not success and result.error:
            # Redacted like everything else that lands in the durable record — a failure message can
            # carry a token or a path. `str(None)` is the trap here: it is the truthy string "None",
            # which would fill the column with the word instead of leaving it empty.
            error_text = str(redact_obj({"e": result.error})["e"])[:2000] or None
        await conn.execute(
            "UPDATE tool_execution SET status=$1, observed=$2, verification=$3, success=$4, "
            "  error=$7, finished_at=now(), duration_ms=$5 WHERE id=$6",
            "verified_success" if success else "verified_failure",
            redact_obj({"output": result.output, "display": result.display, "error": result.error}),
            {"success": verify.success, "detail": verify.detail}, success, duration, exec_id,
            error_text,
        )
        await journal.set_state(RunState.UPDATE_STATE)
        await journal.event(
            "tool",
            {"name": tool.name, "ok": result.ok, "verified": verify.success, "success": success},
            latency_ms=duration,
        )
        await self._audit(
            conn, journal.run_id, tool, call.arguments, decision, result.ok, verify.success, duration
        )
        await self._emit(
            conn, "tool.completed" if success else "tool.failed", journal.run_id,
            {"tool": tool.name, "verified": verify.success, "success": success},
        )

        # Provenance (§10): a tool result IS a live observation made just now — tag it with source +
        # timestamp so the model treats it as current ground truth, never confuses it with a memory, and
        # can prefer it over a stale recall. (ssh_run additionally carries the remote host_id in output.)
        now_iso = self.clock.now().isoformat()
        if success:
            return (
                _tool_message(tool.name, {"ok": True, "source": "live_observation", "observed_at": now_iso,
                                          "output": redact_obj(result.output), "verified": True}),
                True, result.display or tool.name,
            )
        return (
            _tool_message(tool.name, {"ok": False, "source": "live_observation", "observed_at": now_iso,
                                      "error": redact_obj(result.error or verify.detail)}),
            False, result.error or verify.detail or result.display or "failed",
        )

    async def _audit(
        self,
        conn: Any,
        run_id: UUID,
        tool: Any,
        args: dict[str, Any],
        decision: Any,
        ok: bool | None,
        verified: bool | None,
        duration_ms: int | None,
    ) -> None:
        await conn.execute(
            "INSERT INTO tool_audit "
            "  (run_id, tool_name, risk_level, args_redacted, decision, ok, verified, duration_ms) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
            run_id, tool.name, int(decision.risk), redact_obj(args), decision.action.value,
            ok, verified, duration_ms,
        )

    async def _system_critical_binaries(self) -> frozenset[str]:
        """The binaries classified SYSTEM_CRITICAL (§34), cached with a short TTL. On any DB hiccup,
        keep the last-known set (or empty) — the enforcement must never itself break a turn."""
        now = self.clock.now().timestamp()
        if self._syscrit is not None and (now - self._syscrit[1]) < _AUTHORITY_TTL:
            return self._syscrit[0]
        names: frozenset[str] = self._syscrit[0] if self._syscrit else frozenset()
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT t.name FROM tool_authority a JOIN discovered_tool t ON t.id=a.tool_id "
                    "WHERE a.valid_until IS NULL AND a.authority='system_critical' AND t.available")
            names = frozenset(r["name"] for r in rows)
        except Exception as exc:  # noqa: BLE001 - enforcement is best-effort over a cache
            self.log.warning("authority_floor_load_failed", error=str(exc))
        self._syscrit = (names, now)
        return names

    async def _authority_floor(
        self, tool: Any, args: dict[str, Any], decision: PolicyDecision
    ) -> PolicyDecision:
        """Enforce the discovered-tool authority record (§34/§35/§88): a command whose binary is
        classified SYSTEM_CRITICAL pauses to confirm, even if the tool's own risk assessment would
        auto-allow it. Model-external and immutable — the classification comes from the datastore, not
        from anything the model said. Complements exec_tool's command-level destructive check."""
        if decision.action is not Action.AUTO_ALLOW:
            return decision  # already gated or denied — nothing to tighten
        binary = _command_binary(tool.name, args)
        if binary and binary in await self._system_critical_binaries():
            return PolicyDecision(
                Action.CONFIRM,
                f"'{binary}' is classified system-critical — pausing to confirm first",
                RiskLevel.R4)
        return decision

    async def recover(self) -> list[dict[str, str]]:
        """Resolve runs left 'running' by a crash and, where safe, actually CONTINUE them (§9):
          • REPLAY_SAFE (no side effect was in flight) → re-drive the turn.
          • VERIFY_THEN_CONTINUE (a non-idempotent effect was in flight) → independently verify whether
            it landed (probe reality), record that truth on the tool row, then re-drive so Sali carries
            on from a KNOWN state instead of blindly repeating or aborting.
          • ABORT_SURFACE (unknowable) → surface, never guess.
        The old run is always superseded (aborted) and the resume is a fresh, journaled turn. Gated by
        settings.runtime.resume_interrupted and capped by resume_max_runs; stale-guarded to 2 minutes so
        a live run in another process is never touched."""
        resolved: list[dict[str, str]] = []
        to_redrive: list[tuple[Any, str, dict[str, str]]] = []
        async with self.pool.acquire() as conn:
            runs = await conn.fetch(
                "SELECT run_id, session_id, user_input, state, snapshot FROM agent_runs "
                "WHERE status='running' AND updated_at < now() - interval '2 minutes'")
            for row in runs:
                state = RunState(row["state"])
                idempotent: bool | None = None
                interrupted = None
                if state is RunState.EXECUTE_TOOL:
                    interrupted = await conn.fetchrow(
                        "SELECT id, tool_name, plan FROM tool_execution "
                        "WHERE run_id=$1 AND status='executing' ORDER BY started_at DESC LIMIT 1",
                        row["run_id"],
                    )
                    if interrupted is not None:
                        tool = self.registry.get(interrupted["tool_name"])
                        idempotent = tool.idempotent if tool is not None else None
                # Atomically CLAIM this stale run before ANY recovery work: only the process whose
                # conditional UPDATE affects a row proceeds. A concurrently-starting process (a restart, or
                # terminal + API server booting together) loses the race (0 rows) and skips it — so a
                # still-live or interrupted run is never aborted+re-driven twice (Final audit §34).
                claimed = await conn.fetchval(
                    "UPDATE agent_runs SET status='aborted', state=$1, updated_at=now() "
                    "WHERE run_id=$2 AND status='running' RETURNING run_id",
                    RunState.ABORTED.value, row["run_id"])
                if claimed is None:
                    continue
                action = resume_action(state, idempotent)
                # §19: for a non-idempotent effect, re-observe reality to learn whether it landed.
                verified: VerifyResult | None = None
                if action is ResumeAction.VERIFY_THEN_CONTINUE and interrupted is not None:
                    verified = await self._verify_interrupted(conn, interrupted)
                payload: dict[str, Any] = {"action": action.value, "was_state": state.value}
                if verified is not None:
                    payload["verified"] = verified.success
                await conn.execute(
                    "INSERT INTO run_events (run_id, seq, kind, payload) VALUES "
                    "($1, (SELECT coalesce(max(seq),0)+1 FROM run_events WHERE run_id=$1), "
                    "'resumed', $2)",
                    row["run_id"], payload,
                )
                # (the run was already atomically aborted by the CLAIM above)
                out: dict[str, str] = {
                    "run_id": str(row["run_id"]), "was_state": state.value, "action": action.value
                }
                if verified is not None:
                    out["verified"] = str(verified.success)
                resolved.append(out)
                redrivable = action is ResumeAction.REPLAY_SAFE or (
                    action is ResumeAction.VERIFY_THEN_CONTINUE and verified is not None
                )
                if redrivable and row["user_input"] and row["session_id"]:
                    # Carry the internal flag across the restart. A background turn resumed as a
                    # foreground one wrote its own instructions into Almir's transcript as his words.
                    was_internal = bool((row["snapshot"] or {}).get("internal"))
                    to_redrive.append((row["session_id"], row["user_input"], out, was_internal))

        # Re-drive OUTSIDE the recovery connection (each turn takes its own), bounded + best-effort.
        # A re-drive is a CONTINUATION of the same logical work as a fresh, journaled run (new run_id,
        # same session) — the task_id it carries is unchanged; a context reset never forks the task.
        if self.settings.runtime.resume_interrupted:
            for session_id, user_input, out, was_internal in to_redrive[
                    : self.settings.runtime.resume_max_runs]:
                await self._emit_runtime(
                    "runtime.continuation_started",
                    {"was_run": out.get("run_id"), "was_state": out.get("was_state"),
                     "continuation": True},
                    session_id=session_id)
                try:
                    result = await self.run(user_input, session_id=session_id,
                                            internal=was_internal)
                    out["resumed_run"] = str(result.run_id)
                    await self._emit_runtime(
                        "runtime.continuation_completed",
                        {"was_run": out.get("run_id"), "resumed_run": str(result.run_id)},
                        run_id=result.run_id, session_id=session_id)
                except Exception as exc:  # noqa: BLE001 - a failed resume must not crash startup recovery
                    self.log.warning("recover re-drive failed: %s", exc)
        return resolved

    async def note_downtime(self) -> dict[str, Any]:
        """How long Sali was gone, worked out ONCE at startup, before anything overwrites the evidence.

        A restart left no trace of its own duration. Every timestamp simply looked old, which is not
        the same thing: "the last turn was at 09:00" does not tell him whether he answered it and then
        idled, or was killed mid-sentence and has just come back eight hours later. The distinction is
        the whole of §27/§28 — what was missed while he was away can only be judged against a gap he
        knows the size of.

        `sali_state.updated_at` is the evidence, and it is read here because `note_turn` overwrites it
        on the very first turn after boot. Returns the gap and the deadlines that passed inside it."""
        gap_seconds = 0.0
        overdue: list[str] = []
        with contextlib.suppress(Exception):
            async with self.pool.acquire() as conn:
                last_seen = await conn.fetchval("SELECT updated_at FROM sali_state WHERE id")
                if last_seen is not None:
                    gap_seconds = max(0.0, (self.clock.now() - last_seen).total_seconds())
                if gap_seconds > 60:
                    # Only things that came due INSIDE the gap: a deadline that was already overdue
                    # before he went down is not news, and reporting it as newly missed would be a
                    # small fabrication about when it happened.
                    rows = await conn.fetch(
                        "SELECT name, next_run_at FROM schedule "
                        "WHERE enabled AND next_run_at <= $1 AND next_run_at > $2 "
                        "ORDER BY next_run_at LIMIT 10",
                        self.clock.now(), last_seen)
                    overdue = [f"{r['name']} (was due {self.temporal.ago(r['next_run_at'])})"
                               for r in rows]
            self._downtime = {"seconds": gap_seconds, "missed_schedules": overdue}
            self._back_at = self.clock.now()   # so the note can stop mentioning it after an hour
            if gap_seconds > 60:
                await self._emit_runtime(
                    "runtime.downtime",
                    {"down_for": self.temporal.duration(gap_seconds),
                     "seconds": round(gap_seconds), "missed_schedules": overdue})
                self.log.info("downtime_noted", seconds=round(gap_seconds),
                              missed=len(overdue))
        return getattr(self, "_downtime", {"seconds": 0.0, "missed_schedules": []})

    async def recover_tasks(self) -> list[dict[str, Any]]:
        """Detect and recover orphaned tasks after a crash or restart.

        Finds tasks with stale heartbeats (>2 min), marks them interrupted,
        and builds recovery context so they can be resumed.
        """
        from sali.tasks.recovery import detect_orphaned_tasks, mark_task_interrupted, recover_task

        orphaned = await detect_orphaned_tasks(self.pool)
        if not orphaned:
            return []

        await self._emit_runtime("runtime.recovery_started", {"orphaned": len(orphaned)})
        recovered = []
        for info in orphaned:
            task_id = UUID(info["task_id"])
            if not info["can_resume"]:
                # Exhausted retries — mark as failed
                await self._tasks.finish(task_id, status="failed",
                                         result=f"exhausted {info['max_retries']} retries")
                self.log.warning("task_exhausted_retries", task_id=info["task_id"])
                continue

            # Mark as interrupted (preserves primary status)
            await mark_task_interrupted(self.pool, task_id, reason="process_restart")
            with contextlib.suppress(Exception):
                append_event(task_id, "task_interrupted", {"reason": "process_restart"})

            # Build recovery context
            recovery = await recover_task(self.pool, task_id)
            recovered.append(recovery)
            with contextlib.suppress(Exception):
                append_event(task_id, "task_recovered",
                             {"retry_count": recovery.get("retry_count"),
                              "completed_steps": recovery.get("completed_steps")})
            # Deterministic, DB-driven continuation context is now ready for this task (§13) — the next
            # live turn resumes it from durable state via _open_tasks_note, never by asking the model
            # "what were we doing?". Announce it so clients see the recovery land.
            await self._emit_runtime(
                "runtime.recovery_completed",
                {"completed_steps": recovery.get("completed_steps"),
                 "total_steps": recovery.get("total_steps"),
                 "retry_count": recovery.get("retry_count")},
                task_id=task_id)
            self.log.debug("task_recovered", task_id=info["task_id"],
                          objective=info["objective"], retry=recovery.get("retry_count"))

        return recovered

    async def _verify_interrupted(self, conn: Any, exec_row: Any) -> VerifyResult | None:
        """Independently re-observe whether an interrupted non-idempotent tool's effect landed (§9/§19),
        and record that truth on the durable tool_execution row. Currently probes execute_command via the
        shell-effect probes; returns None (→ surface, don't re-drive) for effects we can't check."""
        if exec_row["tool_name"] != "execute_command":
            return None
        args = (exec_row["plan"] or {}).get("args") or {}
        command = args.get("command")
        if isinstance(command, list):
            command = " ".join(str(c) for c in command)
        if not isinstance(command, str) or not command.strip():
            return None
        verdict = await verify_effect(command)
        if verdict is None:
            return None
        await conn.execute(
            "UPDATE tool_execution SET status=$1, verification=$2, success=$3, finished_at=now() "
            "WHERE id=$4",
            "verified_success" if verdict.success else "verified_failure",
            {"success": verdict.success, "detail": verdict.detail, "recovered": True},
            verdict.success, exec_row["id"],
        )
        return verdict


    async def _ensure_conversation(self, conn: Any, session_id: UUID) -> None:
        inserted = await conn.fetchval(
            "INSERT INTO conversation (id, channel) VALUES ($1, 'terminal') "
            "ON CONFLICT (id) DO NOTHING RETURNING id",
            session_id,
        )
        if inserted is not None:
            await self._emit(
                conn, "conversation.created", session_id, {}, subject_type="conversation"
            )

    async def _append_message(
        self, conn: Any, session_id: UUID, role: str, content: str, *, model: str | None = None,
        attachment: dict[str, Any] | None = None,
    ) -> None:
        seq = await conn.fetchval(
            "SELECT coalesce(max(seq),0)+1 FROM message WHERE conversation_id=$1", session_id
        )
        # `attachment` carries a file Sali sent ON this reply — one durable message (the file card lives
        # WITH the reply, not as a separate bubble), so GET /conversation renders it inline and it
        # survives a reload (Almir: the file showed as a separate message + old files bunched on reload).
        import json as _json
        await conn.execute(
            "INSERT INTO message (conversation_id, seq, role, content, model, token_count, attachment) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb)",
            session_id, seq, role, content, model, self.provider.count_tokens(content),
            (_json.dumps(attachment) if attachment else None),
        )
        await conn.execute("UPDATE conversation SET last_active=now() WHERE id=$1", session_id)

    async def _load_history(self, conn: Any, session_id: UUID) -> list[tuple[str, str]]:
        """Prior turns: the running summary (if any) plus the recent verbatim messages."""
        conv = await conn.fetchrow(
            "SELECT summary, summary_through_seq, summary_packet FROM conversation WHERE id=$1", session_id
        )
        summary = conv["summary"] if conv else None
        through = conv["summary_through_seq"] if conv else 0
        rows = await conn.fetch(
            "SELECT seq, role, content FROM message WHERE conversation_id=$1 AND seq > $2 "
            "ORDER BY seq DESC LIMIT 20",
            session_id, through,
        )
        msgs = list(reversed(rows))
        if len(msgs) > 10:
            # Window start anchored to decade boundaries of seq, so consecutive turns share an
            # append-only history prefix (a strict last-10 window slid EVERY turn, which defeated
            # the provider's KV prefix cache — audit).
            w = (msgs[-1]["seq"] // 10) * 10 - 10
            msgs = [m for m in msgs if m["seq"] > max(w, through)]
        history: list[tuple[str, str]] = [(r["role"], r["content"]) for r in msgs]
        # Prefer the structured packet (its task state is deterministic, §11-12); fall back to prose.
        packet = conv["summary_packet"] if conv else None
        earlier = (continuation.render_packet(packet) if packet else "") or summary
        if earlier:
            history.insert(0, ("earlier", earlier))
        return history

    def _context_window(self) -> int:
        """The model's effective context window — from the provider when it reports one, else the
        configured ctx_default (§28). Never assumed unbounded."""
        return context_budget.resolve_limit(self.provider, self.settings)

    def _budget(self, messages: list[ChatMessage]) -> context_budget.ContextBudget:
        """A deterministic snapshot of how full the working prompt is (§7) — drives fold + events."""
        return context_budget.budget_for(self.provider, self.settings, messages)

    def _over_context(self, messages: list[ChatMessage]) -> bool:
        """True when the working prompt is close enough to the model's window that we should fold it
        before the next call. Conservative (over-estimates) so we fold early, never overflow (§7)."""
        if len(messages) <= 3:
            return False  # system + first turn + one more — nothing worth folding yet
        return self._budget(messages).should_compact

    async def _compact_and_continue(
        self, messages: list[ChatMessage], journal: RunJournal, *,
        count: int, before: context_budget.ContextBudget,
        task_id: UUID | None, session_id: UUID | None, reason: str,
        user_input: str | None = None,
    ) -> list[ChatMessage]:
        """One safe compaction cycle: announce it, protect durable state, fold, and report the result.

        The invariant that makes 'compact and continue without losing a thing' true: everything that
        matters is ALREADY durable (task steps, executions, checkpoints, artifacts, progress) before we
        get here, and the deterministic task header in the fold is re-read from the store — so the fold
        can only lose disposable conversational context, never task state (§9). A compaction is recorded
        as task PROGRESS so the watchdog reads a slow fold as work, not as a stuck/orphaned task (§21)."""
        await self._emit_runtime(
            "runtime.context_compaction_started",
            {"count": count, "reason": reason, **before.as_dict()},
            run_id=journal.run_id, task_id=task_id, session_id=session_id)
        if task_id is not None:
            # Keep the watchdog correct across a slow fold: heartbeat (alive) + progress (working).
            with contextlib.suppress(Exception):
                await self._tasks.heartbeat(task_id)
            with contextlib.suppress(Exception):
                await self._tasks.record_progress(task_id, "compaction")
            # A reproducible context checkpoint/manifest BEFORE folding (§21/§23/§25): source IDs +
            # token estimate, never the raw context — enough to reconstruct the working set on restart.
            with contextlib.suppress(Exception):
                await self._write_context_checkpoint(task_id, journal.run_id, reason, before.used)
        folded = await self._fold_messages(messages, user_input=user_input)
        after = self._budget(folded)
        await journal.event(
            "context_folded", {"kept": len(folded), "count": count, "reason": reason,
                               "before": before.used, "after": after.used})
        await self._emit_runtime(
            "runtime.context_compaction_completed",
            {"count": count, "reason": reason, "kept": len(folded),
             "used_before": before.used, "used_after": after.used, "limit": after.limit},
            run_id=journal.run_id, task_id=task_id, session_id=session_id)
        return folded

    async def _write_context_checkpoint(
        self, task_id: UUID, run_id: UUID | None, reason: str, token_estimate: int,
    ) -> None:
        """Persist a reproducible context manifest (§23/§25): the durable source IDs the capsule drew on
        + a token estimate + the capsule version — NOT the raw context. Enough to reconstruct the working
        set after a restart. Best-effort; never breaks a compaction."""
        from sali.runtime.capsule import CONTEXT_VERSION, build_capsule

        task = await self._tasks.get(task_id)
        if task is None:
            return
        cap = await build_capsule(
            self.pool, task, reviewer=self._reviewer, research=self._research_store,
            skills=self._skills, decisions=self._decisions, phases=self._phases)
        cid: UUID | None = None
        async with self.pool.acquire() as conn:
            cid = await conn.fetchval(
                "INSERT INTO context_checkpoint "
                "  (task_id, run_id, step_seq, workspace, context_version, source_ids, token_estimate, reason) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id",
                task_id, run_id, cap.next_step["seq"] if cap.next_step else None,
                cap.workspace_root, CONTEXT_VERSION, cap.source_ids(), token_estimate, reason)
        await self._emit_runtime(
            "context.checkpoint_created",
            {"checkpoint_id": str(cid) if cid else None, "context_version": CONTEXT_VERSION,
             "token_estimate": token_estimate, "reason": reason},
            run_id=run_id, task_id=task_id)

    async def _fold_messages(
        self, messages: list[ChatMessage], *, user_input: str | None = None,
    ) -> list[ChatMessage]:
        """Fold the middle of the working prompt into a compact carry-forward, so Sali continues with a
        small context. Prompt 6: when a task is active the carry-forward is the DETERMINISTIC Task State
        Capsule regenerated from durable storage — so operational state is lossless across the fold (no
        LLM summary, cheaper, reproducible §9/§10/§49). Only pure conversation (no task) falls back to a
        model-written progress note. The prompt may now carry history as real chat messages, so the
        request is NOT positionally messages[1]: when the caller passes ``user_input`` the request is
        re-synthesized verbatim (dropping the stale per-turn context tail — it re-assembles next call);
        without it, fall back to the first user message."""
        system = messages[0]
        if user_input is not None:
            first_user = ChatMessage(role="user", content=user_input)
            body = messages[1:]
        else:
            first_user = next((m for m in messages[1:] if m.role == "user"), messages[1])
            body = [m for m in messages[1:] if m is not first_user]
        task = await self._current_task_safe()
        if task is not None:
            body_note = await self._task_capsule_note(task)  # lossless operational state, from Postgres
        else:
            transcript = "\n".join(f"{m.role}: {(m.content or '')[:2000]}" for m in body)
            note = await self.provider.chat(
                [ChatMessage(role="system", content=continuation.PACKET_INSTRUCTION),
                 ChatMessage(role="user", content=transcript)],
                preset=BALANCED,
            )
            body_note = note.content.strip()
        folded = [
            system, first_user,
            ChatMessage(
                role="user",
                content=f"(Where you are in this task so far — continue from here:\n{body_note})",
            ),
        ]
        # Keep the single most recent concrete result verbatim (as plain context, so there are no
        # orphaned tool-call pairings) — so Sali doesn't redo the step it just finished.
        recent = next((m for m in reversed(body) if (m.content or "").strip()), None)
        if recent is not None:
            folded.append(ChatMessage(
                role="user", content=f"(Your most recent result, in full:\n{(recent.content or '')[:6000]})"
            ))
        return folded

    def _maybe_learn(self) -> None:
        """Fire a throttled, OFF-THREAD consolidation pass (§17-19) so memory actually ACCRUES during
        normal use: mine procedures, record failures→fixes, distill an episode. Fire-and-forget so it
        never adds turn latency; skipped if one is already running or ran within _CONSOLIDATE_EVERY_S.
        Without this the six memory types never come into existence during a normal session."""
        if self.learning is None:
            return
        if self._consolidating is not None and not self._consolidating.done():
            return  # one already in flight — don't stack distillations
        now = self.clock.now()
        if (self._last_consolidate is not None
                and (now - self._last_consolidate).total_seconds() < _CONSOLIDATE_EVERY_S):
            return  # ran recently — throttle
        self._last_consolidate = now
        self._consolidating = asyncio.create_task(self._consolidate_safe())

    async def _consolidate_safe(self) -> None:
        """Run one consolidation pass, swallowing any error — background learning must never crash
        the session, and a distillation hiccup just means we try again next window."""
        try:
            await self.learning.consolidate()
        except Exception as exc:  # noqa: BLE001 - never let background learning surface as a crash
            self.log.warning("consolidate_failed", error=str(exc))

    def _schedule_compact(self, session_id: UUID) -> None:
        """Fire the fold in the background and remember the handle so a rapid follow-up can cancel it.

        Idempotent per session: if a fold is already running (or scheduled) for this session it stays,
        because compaction is incremental and starting a second concurrent fold on the same seq range
        would produce two summaries chasing each other."""
        existing = self._compact_tasks.get(session_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._compact_in_background(session_id))
        self._compact_tasks[session_id] = task

    async def _compact_in_background(self, session_id: UUID) -> None:
        """Run one compaction cycle. Uses its OWN pool connection so the caller's transaction closes
        cleanly the moment the turn does; the model call inside enters `_inference_lease` like any
        other cognition, so a user turn that arrives mid-fold cancels it via
        `_cancel_compact_for_session` before entering context assembly."""
        with contextlib.suppress(asyncio.CancelledError):
            async with self.pool.acquire() as conn:
                with contextlib.suppress(Exception):
                    await self._maybe_compact(conn, session_id)

    async def _cancel_compact_for_session(self, session_id: UUID) -> None:
        """Called at the top of a new turn: if a background fold is running for this session, cancel
        it. Nothing is corrupted by the cancellation because `_maybe_compact` writes the summary in a
        single UPDATE at the end — either the cycle completed and the row is fully updated, or it did
        not and the row is unchanged. Compaction is incremental, so a skipped fold is picked up on
        the next idle window with no loss."""
        task = self._compact_tasks.pop(session_id, None)
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _maybe_compact(self, conn: Any, session_id: UUID) -> None:
        """When the conversation grows long, summarize older turns so the session never breaks."""
        row = await conn.fetchrow(
            "SELECT summary, summary_through_seq, "
            "  (SELECT max(seq) FROM message WHERE conversation_id=$1) AS max_seq, "
            "  (SELECT count(*) FROM message WHERE conversation_id=$1 "
            "     AND seq > summary_through_seq) AS uncompacted, "
            "  (SELECT coalesce(sum(length(content)), 0) FROM message WHERE conversation_id=$1 "
            "     AND seq > summary_through_seq) AS uncompacted_chars "
            "FROM conversation WHERE id=$1",
            session_id,
        )
        if row is None:
            return
        # Compact on SIZE as well as on count. Counting messages alone is the wrong trigger: a handful of
        # very large turns (a pasted log, a folded image description) can fill the context window long
        # before 40 messages accumulate, and the window — not the message count — is what actually breaks.
        # ~4 chars/token is the same conservative estimate the context engine uses.
        approx_tokens = int((row["uncompacted_chars"] or 0) / 4)
        size_trigger = max(2048, int(self.context.budget * _COMPACT_AT_BUDGET_FRACTION))
        if (row["uncompacted"] or 0) < _COMPACT_AFTER and approx_tokens < size_trigger:
            return
        cutoff = int(row["max_seq"]) - _KEEP_RECENT
        if cutoff <= int(row["summary_through_seq"]):
            return
        older = await conn.fetch(
            "SELECT role, content FROM message WHERE conversation_id=$1 AND seq > $2 AND seq <= $3 "
            "ORDER BY seq",
            session_id, row["summary_through_seq"], cutoff,
        )
        transcript = "\n".join(f"{m['role']}: {m['content']}" for m in older)
        prompt = [
            ChatMessage(role="system", content=continuation.PACKET_INSTRUCTION),
            ChatMessage(role="user", content=f"Prior summary:\n{row['summary'] or '(none)'}\n\n"
                                             f"Earlier turns:\n{transcript}"),
        ]
        result = await self.provider.chat(prompt, preset=BALANCED)
        summary_text = result.content.strip()
        # §11-12: store the prose AND a machine-readable packet whose task state is deterministic.
        packet = continuation.build_packet(await self._current_task_safe(), summary_text)
        await conn.execute(
            "UPDATE conversation SET summary=$1, summary_through_seq=$2, summary_packet=$3 WHERE id=$4",
            summary_text, cutoff, packet, session_id,
        )
        await self._emit(conn, "conversation.compacted", session_id,
                         {"through_seq": cutoff}, subject_type="conversation")

    async def _emit_runtime(
        self, event_type: str, data: dict[str, Any], *,
        run_id: UUID | None = None, task_id: UUID | None = None, session_id: UUID | None = None,
    ) -> None:
        """Emit a canonical runtime.* event (context compaction / continuation / recovery) so every
        client — terminal and iPhone — can observe 'Sali is compacting…' then 'Sali continued' (§22).
        Carries task/run/session identity; only operational counts, never prompt content or reasoning.
        Best-effort: observability must never break a turn or a recovery."""
        if self._publisher is None:
            return
        from sali.events.publisher import SaliEvent
        with contextlib.suppress(Exception):
            await self._publisher.publish(SaliEvent(
                event_type=event_type, run_id=run_id, task_id=task_id, session_id=session_id,
                origin="runtime", data=data))

    async def _emit(
        self,
        conn: Any,
        event_type: str,
        subject_id: UUID,
        payload: dict[str, Any],
        *,
        subject_type: str = "agent_run",
    ) -> None:
        """Emit a durable action event via the canonical publisher."""
        if self._publisher is not None:
            from sali.events.publisher import SaliEvent
            with contextlib.suppress(Exception):
                await self._publisher.publish(SaliEvent(
                    event_type=event_type,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    origin="agent",
                    data=payload,
                ))
        else:
            # Fallback: direct SQL (backward compatibility)
            await conn.execute(
                "INSERT INTO event (event_type, subject_type, subject_id, payload) "
                "VALUES ($1,$2,$3,$4)",
                event_type, subject_type, subject_id, payload,
            )


# No single tool result may swallow the whole context window, but Sali is powerful and should SEE
# most of a file/command/ssh output — with 65K context, give it a very generous slice so it can
# reason over full file contents and command outputs. The fold guard still prevents overflow,
# and the full result always lives in the durable record.
_TOOL_OUTPUT_CAP = 48_000


def _tool_message(tool_name: str, payload: dict[str, Any]) -> ChatMessage:
    content = json.dumps(payload)
    if len(content) > _TOOL_OUTPUT_CAP:
        content = content[:_TOOL_OUTPUT_CAP] + f"… [truncated; {len(content)} bytes total]"
    return ChatMessage(role="tool", name=tool_name, content=content)


def _circuit_broken_message(tool_name: str, failures: int, error: str) -> ChatMessage:
    """A factual tool result (not a coaching paragraph) saying this exact call keeps failing and was
    not run again — enough for the model to try something different."""
    return _tool_message(
        tool_name, {"ok": False, "refused": True, "failures": failures, "error": error}
    )
