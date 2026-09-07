"""Task tools — Sali runs persistent, multi-step work that survives restarts (spec §24).

For a job with several steps ("investigate the VPS", "build and deploy the site"), Sali lays it out
with plan_task; the task and its steps are stored durably and shown back in context every turn, so
Sali resumes exactly where it left off even after a restart. advance_task marks progress; the task
finishes on its own when every step is done, or Sali closes it with finish_task.
"""

from __future__ import annotations

import contextlib
import os
import re
from typing import Any
from uuid import UUID

from sali.core.enums import Capability, RiskLevel
from sali.runtime.research_intent import is_research_objective
from sali.tools.base import Tool, ToolResult
from sali.tools.context import ToolContext
from sali.tools.registry import ToolRegistry

# 'blocked' is here because the runtime prompt tells Sali, verbatim, to "call advance_task with
# status='blocked' and say plainly what you need from Almir" — and the tool used to reject exactly
# that call. After three failed attempts the one instruction he is given was the one he could not
# obey. The column already permits it (task_step CHECK allows blocked).
_STEP_STATES = ["done", "failed", "running", "skipped", "blocked"]

# ── Dead-work guard (autonomy) ───────────────────────────────────────────────────────────────────
# An autonomy/continuation turn must never resurrect an objective Almir recently abandoned/revoked, or
# one that is already completed. This closes the 6h tanzhost re-spawn: a task Almir explicitly killed
# ("leave it"/"stop it") was re-created unprompted 5h later by the continuation loop and churned for
# hours. Enforced structurally (the model ignores prompts). A LIVE user request is exempt — only
# autonomous re-creation is refused; Almir asking again is always honoured.
_OBJ_STOP = frozenset({
    "the", "a", "an", "and", "or", "to", "of", "for", "in", "on", "at", "by", "with", "from",
    "find", "get", "check", "about", "info", "information", "s", "his", "her", "their", "then", "tell",
})


def _norm_obj(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split())


def _stem(w: str) -> str:
    """Light plural fold (no stemming library): 'owners' -> 'owner' so a singular/plural rewording of
    a dead objective still matches. Only trims a trailing 's' on tokens longer than 3 chars."""
    return w[:-1] if len(w) > 3 and w.endswith("s") else w


def _obj_tokens(text: str) -> set[str]:
    return {_stem(w) for w in _norm_obj(text).split() if w not in _OBJ_STOP and len(w) > 2}


async def _recently_dead_objective(pool: Any, objective: str, *, window_hours: int = 48) -> str | None:
    """Return a short label (the status/reason) if a task with a matching objective was recently
    abandoned/revoked/cancelled/superseded/completed/failed, else None. Match is normalized-exact OR
    high content-token overlap (Jaccard/containment on plural-folded non-stopword tokens, requiring at
    least two shared content tokens), so a paraphrase of dead work is still caught while a single shared
    word from a one-word objective no longer blanket-blocks. Reads sali.task (covers current rows) and
    revoked_intent (tombstones that survive archival). Best-effort and read-only — any error → None."""
    norm = _norm_obj(objective)
    toks = _obj_tokens(objective)
    if not norm or pool is None:
        return None

    def _matches(other: str) -> bool:
        on = _norm_obj(other)
        if not on:
            return False
        if on == norm:
            return True
        ot = {_stem(w) for w in on.split() if w not in _OBJ_STOP and len(w) > 2}
        if not toks or not ot:
            return False
        inter = len(toks & ot)
        # A fuzzy (non-exact) match needs at least TWO shared content tokens. This closes the one-word
        # blanket-block: a recently-DONE one-word objective (dead tokens {nginx}) used to score
        # containment 1/1 = 1.0 against ANY new objective that merely mentioned that word ("restart the
        # nginx service now"), refusing it for 48h. An identical restatement is still caught above by the
        # normalized-exact check, so a genuine re-spawn of even a one-word objective is unaffected.
        if inter < 2:
            return False
        # Symmetric Jaccard OR asymmetric CONTAINMENT in either direction, so both an EXPANDED
        # restatement (dead fully contained in a bigger new objective — Jaccard drops on the larger
        # union while containment stays high: "scrape tanzhost owners" -> "scrape all the owners and
        # directors from the tanzhost site into a spreadsheet") and a TRIMMED/reworded one (new
        # contained in dead: "scrape tanzhost owners into spreadsheet" -> "get me the tanzhost owner
        # list") are still refused. Plural-folding above lets 'owners'/'owner' count as shared.
        return (inter / len(toks | ot) >= 0.6
                or inter / len(ot) >= 0.8
                or inter / len(toks) >= 0.6)

    with contextlib.suppress(Exception):
        rows = await pool.fetch(
            "SELECT objective, status FROM sali.task "
            "WHERE status IN ('abandoned','cancelled','revoked','superseded','done','failed') "
            "AND updated_at > now() - ($1 * interval '1 hour') "
            "ORDER BY updated_at DESC LIMIT 60",
            window_hours,
        )
        for r in rows:
            if _matches(r["objective"] or ""):
                return str(r["status"])
    with contextlib.suppress(Exception):
        rows = await pool.fetch(
            "SELECT objective, reason FROM sali.revoked_intent "
            "WHERE revoked_at > now() - ($1 * interval '1 hour') ORDER BY revoked_at DESC LIMIT 60",
            window_hours,
        )
        for r in rows:
            if _matches(r["objective"] or ""):
                return str(r["reason"] or "revoked")
    return None

# Step-discipline: verify a step's definition_of_done before accepting advance_task('done').
# The check is deliberately CONSERVATIVE — it only ever BLOCKS when it can concretely prove a named
# file is still missing. If the DoD names no checkable file path, it falls back to trust (the prior
# behaviour), so a legitimate non-file step is never wrongly blocked. This is what makes "Step 6 is
# done!" with nothing on disk structurally impossible: the DoD names the files, and if they aren't
# there, 'done' is refused with the exact missing path.
_PATH_TOKEN = re.compile(
    r"""[`"']?                              # optional opening quote/backtick
    (                                        # a path-ish token:
      (?:[\w.\-/]+/)?[\w.\-]+                 #   optional dir segments + a filename
      \.(?:py|php|blade\.php|ts|tsx|js|jsx|vue|sql|json|ya?ml|md|txt|html|css|scss|
          go|rs|java|rb|c|cpp|h|hpp|sh|toml|ini|cfg|env|xml|kt|swift|dart|ex|exs)
    )
    [`"']?""",
    re.VERBOSE | re.IGNORECASE,
)


def _extract_paths(text: str) -> list[str]:
    """Pull file-path-looking tokens out of a definition_of_done / description. Bounded + de-duped."""
    if not text:
        return []
    seen: list[str] = []
    for m in _PATH_TOKEN.finditer(text):
        p = m.group(1).strip().strip("`\"'")
        if p and p not in seen and 3 <= len(p) <= 300:
            seen.append(p)
        if len(seen) >= 12:
            break
    return seen


def _verify_definition_of_done(task: Any, step_seq: int) -> tuple[bool, str]:
    """Return (ok, reason). ok=False ONLY when the step's DoD names files and at least one does not
    exist on disk yet. Absent DoD or no extractable path → (True, "") (trust, as before)."""
    step = next((s for s in (getattr(task, "steps", None) or [])
                 if getattr(s, "seq", None) == step_seq), None)
    if step is None:
        return True, ""
    dod = (getattr(step, "definition_of_done", None) or "").strip()
    if not dod:
        return True, ""
    paths = _extract_paths(dod)
    if not paths:
        return True, ""  # nothing structurally checkable — fall back to trust
    root = getattr(task, "workspace_root", None)
    roots = [root] if root else []
    roots += list(getattr(task, "allowed_write_roots", None) or [])
    missing: list[str] = []
    linked: list[str] = []
    for p in paths:
        candidates = [p] if os.path.isabs(p) else [
            os.path.join(r, p) for r in roots if r
        ] or [p]
        # A symlink is NOT the file. os.path.exists() follows links, so pointing this path at a file
        # in another directory used to satisfy the gate — which is exactly how one deliverable ended
        # up split across two folders, half of it links. The step asked for a file HERE.
        if any(os.path.exists(c) and not os.path.islink(c) for c in candidates):
            continue
        if any(os.path.islink(c) for c in candidates):
            linked.append(p)
        else:
            missing.append(p)
    if missing:
        return False, ("not done yet — the definition of done names files that do not exist on disk: "
                       + ", ".join(missing[:6])
                       + ". Create them with real tool calls, then mark the step done.")
    if linked:
        return False, ("not done yet — these are symlinks, not files you created here: "
                       + ", ".join(linked[:6])
                       + ". If the work really lives elsewhere, say so and change the step; do not "
                         "satisfy the path with a link.")
    return True, ""



async def _folder(ctx: ToolContext, method: str, *args: Any, **kwargs: Any) -> Any:
    """Best-effort task-folder mirror op via the task store, so tools never import the tasks layer.
    No-ops silently when the sink does not provide it (e.g. a test fake)."""
    import contextlib
    fn = getattr(getattr(ctx, "tasks", None), method, None)
    if fn is None:
        return None
    with contextlib.suppress(Exception):
        return await fn(*args, **kwargs)
    return None

_STEP_LINE = re.compile(r"^\s*(?:step\s*)?(?:\(?\d+[.):]|[-*•])\s*(.+)$", re.IGNORECASE)


def _parse_prose_steps(text: str) -> list[str]:
    """Pull ordered steps out of a prose / numbered string. Handles three shapes: one step per line
    ("1. do X\\n2. do Y" or "- a\\n- b"), several numbered steps INLINE on one line
    ("1. a 2. b 3. c"), and plain multi-line prose. Empty when it can't find ≥2 real steps."""
    if not text:
        return []
    norm = text.replace("\\n", "\n")
    lines = [ln.strip() for ln in norm.splitlines() if ln.strip()]
    numbered: list[str] = []
    for ln in lines:
        m = _STEP_LINE.match(ln)
        if m and m.group(1).strip():
            numbered.append(m.group(1).strip())
    if len(numbered) >= 2:
        return numbered
    # Several numbered steps crammed onto ONE line: split on the "N." / "N)" markers.
    inline = [p.strip() for p in re.split(r"(?:^|\s)\(?\d+[.):]\s*", norm) if p.strip()]
    if len(inline) >= 2:
        return inline
    if numbered:
        return numbered
    return lines if len(lines) >= 2 else []


def _derive_steps(raw: Any, objective: str) -> list[Any]:
    """Turn whatever the model passed for `steps` into a clean ordered list. Accepts a proper array
    (strings or {description, substeps, definition_of_done, …} dicts), a prose/numbered STRING, or —
    last resort — a "Steps:" section folded into the objective. A weak model that can't build the
    structured array still gets a real, watchable task instead of a hard schema rejection."""
    if isinstance(raw, list):
        out = [
            s if isinstance(s, dict) else str(s).strip()
            for s in raw
            if (isinstance(s, dict) and (s.get("description") or s.get("step")))
            or (not isinstance(s, dict) and str(s).strip())
        ]
        if out:
            return out
    if isinstance(raw, str) and raw.strip():
        parsed = _parse_prose_steps(raw)
        if parsed:
            return parsed
    m = re.search(r"\bsteps?\s*:\s*(.+)$", objective, re.IGNORECASE | re.DOTALL)
    if m:
        parsed = _parse_prose_steps(m.group(1))
        if parsed:
            return parsed
    return []


def _enrich_steps(steps: list[Any]) -> list[Any]:
    """When the model omits definition_of_done, derive a checkable one from any file paths the step's
    description names ("create app.py" → "app.py exists"), and set scope_excludes to the file targets
    of LATER steps — so the engine's DoD-verification + scope guard have teeth even on a plan the weak
    model left thin. Steps that name no files are left exactly as-is (nothing invented). Preserves
    substeps/depends_on/etc."""
    norm: list[dict[str, Any]] = [dict(s) if isinstance(s, dict) else {"description": str(s)}
                                  for s in steps]
    own = [_extract_paths(f"{d.get('description', '')} {d.get('definition_of_done', '') or ''}")
           for d in norm]
    for i, d in enumerate(norm):
        if not d.get("definition_of_done") and own[i]:
            d["definition_of_done"] = "these files exist: " + ", ".join(own[i][:6])
        if not d.get("scope_excludes"):
            later = sorted({p for j in range(i + 1, len(norm)) for p in own[j] if p not in own[i]})
            if later:
                d["scope_excludes"] = "files for later steps: " + ", ".join(later[:10])
    return norm


class PlanTask(Tool):
    name = "plan_task"
    description = (
        "Start a persistent, multi-step task so you can carry it across turns (and restarts) without "
        "losing your place. Give the objective and the ordered steps.\n"
        "USE THIS whenever the request needs more than one real action — anything you would naturally "
        "do in several steps: building or changing a project, an investigation, research plus a "
        "write-up, setting something up and verifying it. If you catch yourself about to do three or "
        "four things in one reply, plan it instead.\n"
        "WHY it matters, not just bookkeeping: a planned task runs in the BACKGROUND, so Almir keeps a "
        "responsive chat and can talk to you about anything else while it proceeds; he can watch the "
        "steps tick over on his Tasks screen; and the work survives a restart because it is stored "
        "durably. Doing multi-step work inline instead blocks his chat behind a spinner and leaves him "
        "with no visibility.\n"
        "Do NOT use it for a genuine one-shot answer (a question, a single small edit, a lookup).\n"
        "For filesystem/project work, pass workspace_root to declare the project folder — "
        "all file operations will be confined to that folder."
    )
    parameters = {
        "type": "object",
        "properties": {
            "objective": {"type": "string", "description": "What the whole task is for, in one line."},
            "steps": {
                # Accept an ARRAY (ideal) OR a plain STRING of prose/numbered steps — a weaker model
                # reliably writes "1. … 2. …" as text but often fails to build the structured array,
                # and a hard schema rejection there just made it give up and build inline (the exact
                # thing tasks exist to prevent). run() parses a string into steps; `items` constrains
                # the array case only.
                "type": ["array", "string"],
                "description": (
                    "The ordered steps, as an array — or, if that's easier, a single string with one "
                    "step per line / numbered '1. … 2. …' (it will be parsed). Each step should have a "
                    "definition_of_done (how the engine will KNOW this step is complete — e.g. the "
                    "exact files that must exist, or the command that must succeed), and, when it "
                    "genuinely breaks into smaller pieces, substeps. Add scope_excludes to name what "
                    "this step must NOT touch — the work that belongs to LATER steps — so you cannot "
                    "run ahead. A plain string is still accepted for a trivial step, but a real "
                    "build/investigation step should carry its definition_of_done: you will be handed "
                    "ONE step at a time and it is not accepted as done until its definition_of_done is "
                    "met, so vague steps stall. substeps appear indented under their parent on Almir's "
                    "Tasks screen and are each driven on their own turn."
                ),
                "items": {
                    "anyOf": [
                        {"type": "string"},
                        {
                            "type": "object",
                            "properties": {
                                "description": {"type": "string"},
                                "definition_of_done": {
                                    "type": "string",
                                    "description": "Concrete, checkable: what must be true/exist for "
                                    "this step to count as done.",
                                },
                                "scope_excludes": {
                                    "type": "string",
                                    "description": "What this step must NOT do — later steps' work.",
                                },
                                "substeps": {
                                    "type": "array",
                                    "items": {
                                        "anyOf": [
                                            {"type": "string"},
                                            {
                                                "type": "object",
                                                "properties": {
                                                    "description": {"type": "string"},
                                                    "definition_of_done": {"type": "string"},
                                                    "scope_excludes": {"type": "string"},
                                                },
                                                "required": ["description"],
                                            },
                                        ]
                                    },
                                },
                            },
                            "required": ["description"],
                        },
                    ]
                },
            },
            "workspace_root": {"type": "string",
                               "description": "Optional: the project folder for this task. "
                               "All file writes will be confined to this folder."},
        },
        # Only the objective is strictly required. steps may be an array, a prose string, or even
        # be folded into the objective ("Build X. Steps: 1. …") — run() derives them robustly and
        # only errors (never a bare schema rejection) if it truly can't find any.
        "required": ["objective"],
    }
    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.WRITE})
    idempotent = False

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        objective = str(args.get("objective", "")).strip()
        # RESEARCH IS NOT A TASK. A research/explanation request must be answered in the CHAT — a summary
        # there + a downloadable full report — never turned into a hidden multi-step background task that
        # produces no chat output and can drag on for hours (Almir: "just research not a website task …
        # he must write in the chat and a file to download"; caught live as the 3-step GEA/GEO task).
        # Deterministic classifier (Q2_K ignores prompts); build requests (website/app/script) still plan
        # normally. Refusing steers Sali to look it up inline and reply with a concise summary.
        if objective and is_research_objective(objective):
            return ToolResult(
                ok=False, display="answer in chat",
                error=("This is a question / information request, not a build task — do NOT plan a task "
                       "for it (Almir: normally we just chat, no research task). Answer it directly in "
                       "your reply. If you need current facts, look them up with web_search / web_fetch "
                       "first, then reply concisely in the chat."))
        # A step may legitimately be a STRING or an object carrying substeps/depends_on — the store's
        # `_insert` reads `spec["description"]` and `spec["substeps"]` for exactly that. Coercing
        # everything with str() first meant a structured step arrived as the TEXT of a Python dict, and
        # the substep support could never receive one. Live evidence:
        #   step 1 description = {'description': 'Write tar_archive.py in /home/almir/Desktop/tar-test'}
        # — unreadable on the Tasks screen and unreadable to Sali on the next continuation turn.
        # Derive steps from an array, a prose string, or a "Steps:" section folded into the objective
        # — a weak model that writes "1. … 2. …" as text still gets a real task (see _derive_steps) —
        # then enrich each with a checkable definition_of_done + scope_excludes derived from the files
        # it names, so the engine can verify/advance/scope-guard even a thin plan (see _enrich_steps).
        steps = _enrich_steps(_derive_steps(args.get("steps"), objective))
        # NEVER re-plan work that is already planned. A background continuation turn is handed its
        # task's objective; if the model answers by calling plan_task instead of working the step, the
        # new task becomes primary and the next continuation re-plans it again — three identical
        # "Build a Flask web app…" tasks appeared in three minutes with nothing external asking for
        # them. Refusing an exact duplicate stops the loop at its source while leaving a genuinely new
        # objective (or an explicit replacement) untouched.
        if ctx.tasks is not None and objective:
            with contextlib.suppress(Exception):
                _cur = await ctx.tasks.current()
                if _cur is not None and (getattr(_cur, "objective", "") or "").strip() == objective:
                    return ToolResult(
                        ok=False, display="already planned",
                        error=("that task already exists and is active — work its next unfinished step "
                               "with advance_task instead of planning it again"))
                # And not just an identical objective. Planning ANYTHING while a task is mid-flight
                # supersedes it: `TaskAuthority.activate_task` calls `supersede(current, new)` whenever a
                # current active task exists. Observed live — Sali, one step into its own task, planned a
                # near-identical second one and killed the first, which was left "running" with nobody
                # working it. The prompt-level fix stops the usual trigger; this is the structural one,
                # because a plan must never be able to abandon work that is still moving.
                if _cur is not None and getattr(_cur, "status", "") == "running":
                    _unfinished = [
                        st for st in (getattr(_cur, "steps", None) or [])
                        if getattr(st, "status", "") in ("pending", "running", "waiting", "blocked")
                    ]
                    if _unfinished:
                        _next = _unfinished[0]
                        return ToolResult(
                            ok=False, display="a task is already running",
                            error=("you are already working a task — "
                                   f"\"{(getattr(_cur, 'objective', '') or '')[:80]}\" — and step "
                                   f"{getattr(_next, 'seq', '?')} is still unfinished. Planning now would "
                                   "abandon it. Work that step with advance_task; if this really is a "
                                   "different job, finish or ask Almir about the current one first."))
        # Dead-work guard (autonomy): a continuation/internal turn must NOT resurrect an objective Almir
        # recently abandoned/revoked or that is already completed — the source of the 6h tanzhost
        # re-spawn (a killed task re-created unprompted 5h later). A LIVE user request is exempt: Almir
        # asking again is always honoured. Structural, because the model ignores a prompt telling it not to.
        if getattr(ctx, "internal", False) and objective and ctx.pool is not None:
            _dead = await _recently_dead_objective(ctx.pool, objective)
            if _dead:
                return ToolResult(
                    ok=False, display="already handled",
                    error=(f"not re-creating this on my own — the same objective was '{_dead}' recently. "
                           "If it needs doing again, it waits for Almir to ask."))
        if not objective or not steps:
            return ToolResult(
                ok=False, display="need objective + steps",
                error=("give an objective and at least one step — pass steps as an array, or as a "
                       "single string with one step per line / numbered '1. … 2. …'"))
        if ctx.is_subagent:
            return ToolResult(ok=False, display="not for a subagent",
                              error="a subagent cannot manage tasks — report findings to the primary (§16)")
        if ctx.tasks is None:
            return ToolResult(ok=False, display="no tasks", error="the task engine isn't available")

        # Create the task, then bind its AUTHORITATIVE workspace (Prompt 5): an explicit workspace the
        # user gave → a project folder referenced in the objective → otherwise a deterministic
        # workspace under sali-works/tasks/<task_id>. Never /home directly. This is durable task state,
        # resolved ONCE here and never re-resolved on continuation/recovery/rework.
        explicit_ws = str(args.get("workspace_root", "")).strip() or None
        task = await ctx.tasks.create(objective, steps)
        ws_root = explicit_ws
        with contextlib.suppress(Exception):
            bound = await ctx.tasks.bind_workspace(
                task.id, objective=objective, explicit=explicit_ws,
                sali_works_root=str(ctx.settings.permissions.workspace),
                cwd=str(ctx.settings.permissions.exec_cwd))
            ws_root = bound.get("workspace_root")
        # Activate through the authority system — this handles superseding the old task.
        if ctx.task_authority is not None:
            await ctx.task_authority.activate_task(task.id)
        elif hasattr(ctx.tasks, 'activate'):
            await ctx.tasks.activate(task.id)
        # Log task creation to sali-works (filesystem backup).
        with contextlib.suppress(Exception):
            await _folder(ctx, "save_meta", task.id, objective, steps, workspace_root=ws_root, status="open")
            await _folder(ctx, "log_event", task.id, "task_created",
                         {"objective": objective, "steps": len(steps),
                          "workspace_root": ws_root})
        # HAND OFF instead of executing inline. Before this, the foreground turn planned the task and
        # then did ALL the work in the same turn, holding the one cognition slot — a real request blocked
        # Almir's chat for ~4 minutes behind a spinner. The daemon's task-continuation faculty picks a
        # planned task up within ~45s and works it step by step, yielding the moment Almir speaks. So the
        # right move here is to stop, tell him it is underway, and let the background do it: progress
        # shows on the Tasks screen, and TaskStore.finish announces completion back into the chat.
        return ToolResult(
            ok=True,
            output={"objective": objective, "steps": steps, "count": len(steps),
                    "workspace_root": ws_root,
                    "next": ("STOP HERE — do NOT execute these steps in this turn. A background worker "
                             "picks this task up within a minute and works it step by step, which is "
                             "what keeps Almir's chat responsive. Reply to him now in one or two "
                             "sentences: you're starting it, name the objective, and say you'll report "
                             "when it's done.")},
            display=f"planned '{objective}' ({len(steps)} steps) — background worker takes it from here"
            + (f" in {ws_root}" if ws_root else ""),
        )


class AdvanceTask(Tool):
    name = "advance_task"
    description = (
        "Update your progress on the current task: mark a step done, failed, running, skipped, "
        "or blocked (with a note of what happened — for failed or blocked, say what stopped "
        "you, because that note is what Sali researches later to unblock the step). The task "
        "completes on its own once every step is done. Steps are numbered from 1."
    )
    parameters = {
        "type": "object",
        "properties": {
            "step": {"type": "integer", "description": "The step number (1-based)."},
            "status": {"type": "string", "enum": _STEP_STATES},
            "note": {"type": "string", "description": "What happened on this step. For 'failed' or "
                     "'blocked' this is required in practice: it becomes the error Sali researches "
                     "in free time to get the step moving again."},
            "checkpoint": {"type": "object", "description": "Optional: save in-step progress (any keys) "
                           "so you resume MID-step, not from scratch, after a restart."},
        },
        "required": ["step", "status"],
    }
    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.WRITE})
    idempotent = False

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.is_subagent:
            return ToolResult(ok=False, display="not for a subagent",
                              error="a subagent cannot manage tasks — report findings to the primary (§16)")
        if ctx.tasks is None:
            return ToolResult(ok=False, display="no tasks", error="the task engine isn't available")
        task = await ctx.tasks.current()
        if task is None:
            return ToolResult(ok=False, display="no open task", error="there's no open task to advance")
        step = int(args.get("step") or 0)
        status = str(args.get("status", "")).strip()
        if status not in _STEP_STATES:
            return ToolResult(ok=False, display="bad status", error=f"status must be one of {_STEP_STATES}")
        # IN ORDER, NOTHING SKIPPED. Almir's standing rule: "sali must follow the task steps never skip
        # and each mark it!" Marking step 4 while step 2 is still pending leaves a plan that reads as
        # progress but has holes in it, and the holes are invisible on the Tasks screen — the step count
        # goes up while the work did not happen. So a step can only be advanced when every earlier step
        # is settled. A step genuinely not needed is still marked explicitly, as 'skipped', in its turn.
        _unfinished = [
            st for st in (getattr(task, "steps", None) or [])
            if getattr(st, "seq", 0) < step
            and getattr(st, "status", "") in ("pending", "running", "waiting", "blocked")
        ]
        if _unfinished and status in ("done", "skipped"):
            _first = _unfinished[0]
            return ToolResult(
                ok=False, display=f"step {getattr(_first, 'seq', '?')} comes first",
                error=(f"step {getattr(_first, 'seq', '?')} is still "
                       f"{getattr(_first, 'status', 'unfinished')} — "
                       f"\"{(getattr(_first, 'description', '') or '')[:70]}\". Work and mark that one "
                       f"before step {step}; steps are done in order and every one gets marked."))
        # STEP-DISCIPLINE: verify the step's definition_of_done before accepting 'done'. If the DoD
        # names files that are not on disk, the mark is REFUSED — this is what makes a false "Step 6 is
        # done!" (with nothing written) structurally impossible, rather than trusting the model's word.
        # Only ever blocks on concretely-missing named files; a DoD with no checkable path falls back
        # to trust, so a legitimate non-file step is never wrongly held open.
        if status == "done":
            _dod_ok, _dod_reason = _verify_definition_of_done(task, step)
            if not _dod_ok:
                return ToolResult(ok=False, display=f"step {step} not done yet", error=_dod_reason)
        note = str(args.get("note", "")).strip() or None
        # §5: persist in-step progress so a long step resumes mid-way after a restart, not from scratch.
        checkpoint = args.get("checkpoint")
        if isinstance(checkpoint, dict) and checkpoint:
            with contextlib.suppress(Exception):
                await ctx.tasks.checkpoint(task.id, step, checkpoint)
        # §8: when a step is DONE, link the tool_execution that VERIFIED an effect this run — so the
        # step is provably "verified", not merely "reported done". Best-effort; never blocks the mark.
        verified_by: UUID | None = None
        if status == "done" and ctx.pool is not None and ctx.run_id is not None:
            with contextlib.suppress(Exception):
                async with ctx.pool.acquire() as conn:
                    verified_by = await conn.fetchval(
                        "SELECT id FROM tool_execution WHERE run_id=$1 AND status='verified_success' "
                        "ORDER BY finished_at DESC LIMIT 1",
                        ctx.run_id,
                    )
        updated, err = await ctx.tasks.advance(
            task.id, step, status, note=note, verified_by=verified_by, run_id=ctx.run_id)
        if err is not None:
            # Validation failed — return clear error to the LLM
            return ToolResult(ok=False, display=f"step {step} rejected", error=err)
        if updated is None:
            # Task auto-completed and was archived to sali-works/tasks/
            return ToolResult(
                ok=True,
                output={"objective": task.objective, "task_status": "done",
                        "step": step, "step_status": status},
                display=f"step {step} → {status} (task done — archived)",
            )
        # Log step advancement to sali-works (filesystem backup).
        with contextlib.suppress(Exception):
            await _folder(ctx, "log_event", task.id, "step_advanced",
                         {"step": step, "status": status, "note": note,
                          "task_status": updated.status})
        # All steps are complete but the task did NOT auto-archive → the reviewer gate is in effect
        # (Prompt 4): the task is verified by review, never by marking the last step done. Tell the
        # model to run the review rather than assume completion. Surfaces the latest review's findings.
        if updated.next_step is None and updated.status == "running":
            review = None
            with contextlib.suppress(Exception):
                if ctx.reviewer is not None:
                    latest = await ctx.reviewer.latest_review(task.id)
                    review = latest.to_public() if latest is not None else None
            return ToolResult(
                ok=True,
                output={"objective": updated.objective, "task_status": "running",
                        "step": step, "step_status": status, "all_steps_done": True,
                        "review_required": True, "review": review},
                display=f"step {step} → {status} (all steps done — run finish_task to review & complete)",
            )
        return ToolResult(
            ok=True,
            output={"objective": updated.objective, "task_status": updated.status,
                    "step": step, "step_status": status},
            display=f"step {step} → {status}"
            + (" (task done)" if updated.status == "done" else ""),
        )


class FinishTask(Tool):
    name = "finish_task"
    description = (
        "Close the current task explicitly — 'done' when it's complete, or 'abandoned' if you're "
        "stopping it — with a short result summary. (A task also finishes on its own when all its "
        "steps are done.)"
    )
    parameters = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["done", "abandoned", "failed"]},
            "result": {"type": "string", "description": "A short summary of the outcome."},
        },
        "required": ["status"],
    }
    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.WRITE})
    idempotent = False

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.is_subagent:
            return ToolResult(ok=False, display="not for a subagent",
                              error="a subagent cannot manage tasks — report findings to the primary (§16)")
        if ctx.tasks is None:
            return ToolResult(ok=False, display="no tasks", error="the task engine isn't available")
        task = await ctx.tasks.current()
        if task is None:
            return ToolResult(ok=False, display="no open task", error="there's no open task to finish")
        status = str(args.get("status", "done")).strip()
        result = str(args.get("result", "")).strip() or None

        # The reviewer gate (Prompt 4) lives in the store (the bypass-proof choke point): finish('done')
        # runs a FRESH deterministic review and completes only on PASS. Here we surface a rejection with
        # the concrete findings — read back from the durable review the store just wrote — so the model
        # knows exactly what to fix (same task, no new task). NEEDS_REWORK / BLOCKED keep the task open.
        err = await ctx.tasks.finish(task.id, status=status, result=result, run_id=ctx.run_id)
        if err is not None:
            if err == "review_required" and ctx.reviewer is not None:
                latest = await ctx.reviewer.latest_review(task.id)
                if latest is not None:
                    pub = latest.to_public()
                    verb = "blocked" if latest.status.value == "blocked" else "needs rework"
                    return ToolResult(
                        ok=False,
                        output={"status": "rejected", "reason": "review_required",
                                "task_id": str(task.id), "review_id": str(latest.review_id),
                                "review_status": latest.status.value, "summary": latest.summary,
                                "failed": pub["failures"], "rework": pub["recommendations"]},
                        display=f"review: {verb} — {len(pub['failures'])} issue(s) to fix",
                        error=f"completion rejected — {verb}: {latest.summary}",
                    )
            return ToolResult(ok=False, display="cannot finish task", error=err)
        # The reviewer passed and the task is done — promote any VERIFIED learning candidates into
        # durable memory (Prompt 5 §19). A failed/abandoned task promotes nothing.
        if status == "done" and ctx.research is not None:
            with contextlib.suppress(Exception):
                await ctx.research.promote_verified()
        # Log task completion to sali-works (filesystem backup).
        with contextlib.suppress(Exception):
            await _folder(ctx, "log_event", task.id, "task_finished",
                         {"status": status, "result": result})
        return ToolResult(ok=True, output={"objective": task.objective, "status": status},
                          display=f"task {status}"
                          + (" (review passed)" if status == "done" and ctx.reviewer is not None else ""))


class ReviewTask(Tool):
    name = "review_task"
    description = (
        "Verify — with real evidence — whether the current task is actually complete, the way a careful "
        "engineer checks their own work before declaring it done. This inspects durable state (which "
        "steps are verified, tool executions, artifacts on disk) and returns PASS / NEEDS_REWORK / "
        "BLOCKED with concrete findings. Use it to check before finishing; finish_task also runs it "
        "automatically. It never marks the task done — it only reports what the evidence shows."
    )
    parameters = {"type": "object", "properties": {}}
    risk_level = RiskLevel.R0
    capabilities = frozenset()
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.is_subagent:
            return ToolResult(ok=False, display="not for a subagent",
                              error="a subagent cannot manage tasks — report findings to the primary (§16)")
        if ctx.tasks is None:
            return ToolResult(ok=False, display="no tasks", error="the task engine isn't available")
        task = await ctx.tasks.current()
        if task is None:
            return ToolResult(ok=False, display="no open task", error="there's no open task to review")
        if ctx.reviewer is None:
            return ToolResult(ok=False, display="no reviewer",
                              error="the reviewer isn't available in this context")
        review = await ctx.reviewer.review(task.id, run_id=ctx.run_id)
        pub = review.to_public()
        return ToolResult(
            ok=True,
            output={"task_id": str(task.id), "review_id": str(review.review_id),
                    "status": review.status.value, "attempt": review.attempt,
                    "summary": review.summary, "passed": review.passed, "failed": review.failed,
                    "unknown": review.unknown, "failures": pub["failures"],
                    "rework": pub["recommendations"]},
            display=f"review: {review.status.value} — {review.summary}",
        )


class ConfirmTask(Tool):
    name = "confirm_task"
    description = (
        "You're satisfied with the task result — confirm it and clean up the task folder from "
        "sali-works/tasks/. Call this ONLY when Almir says he's happy with the result, or when "
        "you've verified the output is correct. The memory of this task persists in your learning "
        "pipeline even after the folder is deleted."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "The task UUID to confirm. "
                        "Use the current active task's ID."},
            "satisfaction": {"type": "string",
                             "description": "Why you're confirming — e.g. 'Almir confirmed', "
                             "'verified output matches requirements'."},
        },
        "required": ["task_id"],
    }
    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.WRITE})
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        from uuid import UUID

        task_id_str = str(args.get("task_id", "")).strip()
        if not task_id_str:
            return ToolResult(ok=False, display="need task_id", error="task_id is required")
        try:
            task_id = UUID(task_id_str)
        except ValueError:
            return ToolResult(ok=False, display="bad task_id", error="invalid UUID")
        satisfaction = str(args.get("satisfaction", "")).strip() or "confirmed"
        # Log the confirmation
        with contextlib.suppress(Exception):
            await _folder(ctx, "log_event", task_id, "task_confirmed", {"satisfaction": satisfaction})
        # Delete the task folder from sali-works/tasks/
        deleted = await _folder(ctx, "delete_folder", task_id)
        return ToolResult(
            ok=True,
            output={"task_id": task_id_str, "folder_deleted": deleted,
                    "satisfaction": satisfaction},
            display=f"task confirmed — folder {'deleted' if deleted else 'already gone'}",
        )


# Turn 6: publish task.modified on the bus (not just folder-log). Every ModifyTask action calls
# this so iOS + WS replay see the change. Kept beside the folder-log as a durable breadcrumb.
async def _emit_task_modified(conn: Any, ctx: Any, task_id: Any, payload: dict) -> None:
    """Emit task.modified on the SAME conn as the row change (Turn 6 hardening: fix #9).
    Both commit atomically - no observer sees the frame before the durable state.
    Best-effort: a broken publisher never blocks the mutation."""
    with contextlib.suppress(Exception):
        from sali.tasks.store import _emit_task
        pub = getattr(getattr(ctx, "tasks", None), "_publisher", None)
        await _emit_task(conn, "task.modified", task_id, payload, publisher=pub)


class ModifyTask(Tool):
    name = "modify_task"
    description = (
        "Modify the current task's steps when Almir asks for changes. Use this when Almir says "
        "'change X', 'add a step', 'remove that', 'redo step 3', or 'modify the plan'. "
        "You can add, remove, or replace steps. The task keeps its existing progress — "
        "only the specified steps change. This is for mid-task modifications, not new tasks."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string",
                       "enum": ["add", "remove", "replace", "reset", "edit_step", "change_objective"],
                       "description": "add: new step at end or after_step; remove: delete a step; "
                       "replace: change description AND reset execution state (destructive); "
                       "edit_step: change description ONLY, keep status/attempts/checkpoint "
                       "(Turn 6, non-destructive - use this for small wording tweaks); "
                       "reset: mark a step pending again; change_objective: rename the task."},
            "step": {"type": "integer",
                     "description": "The step number (1-based). Required for remove/replace/reset/edit_step."},
            "description": {"type": "string",
                            "description": "New step description. Required for add/replace/edit_step. "
                            "For change_objective: the new task title."},
            "after_step": {"type": "integer",
                           "description": "For 'add': insert after this step number. "
                           "Omit to append at the end."},
        },
        "required": ["action"],
    }
    risk_level = RiskLevel.R1
    capabilities = frozenset({Capability.WRITE})
    idempotent = False

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.is_subagent:
            return ToolResult(ok=False, display="not for a subagent",
                              error="a subagent cannot manage tasks — report findings to the primary (§16)")
        if ctx.tasks is None:
            return ToolResult(ok=False, display="no tasks", error="the task engine isn't available")
        if ctx.pool is None:
            return ToolResult(ok=False, display="no pool", error="database pool not available")
        task = await ctx.tasks.current()
        if task is None:
            return ToolResult(ok=False, display="no open task",
                              error="there's no open task to modify")
        action = str(args.get("action", "")).strip()
        step_num = args.get("step")
        desc = str(args.get("description", "")).strip() or None
        after_step = args.get("after_step")
        async with ctx.pool.acquire() as conn, conn.transaction():
            if action == "add":
                if not desc:
                    return ToolResult(ok=False, display="need description",
                                      error="description is required for add")
                # Determine seq for new step
                if after_step is not None:
                    new_seq = int(after_step) + 1
                    # Shift existing steps after the insertion point up by one, COLLISION-FREE. A single
                    # `seq = seq + 1` violates UNIQUE(task_id,seq) transiently (3→4 while 4 exists), and
                    # `ORDER BY` is not valid on UPDATE in PostgreSQL (Final audit) — so move the affected
                    # rows into the negative domain, then back as +1 (both statements are collision-free
                    # and happen inside this transaction, so intermediate negatives are never visible).
                    await conn.execute(
                        "UPDATE task_step SET seq = -seq WHERE task_id = $1 AND seq > $2",
                        task.id, int(after_step))
                    await conn.execute(
                        "UPDATE task_step SET seq = (-seq) + 1 WHERE task_id = $1 AND seq < 0",
                        task.id)
                else:
                    max_seq = await conn.fetchval(
                        "SELECT coalesce(max(seq), 0) FROM task_step WHERE task_id = $1",
                        task.id)
                    new_seq = max_seq + 1
                await conn.execute(
                    "INSERT INTO task_step (task_id, seq, description) VALUES ($1, $2, $3)",
                    task.id, new_seq, desc)
                with contextlib.suppress(Exception):
                    await _folder(ctx, "log_event", task.id, "task_modified",
                                 {"action": "add", "step": new_seq, "description": desc})
                await _emit_task_modified(conn, ctx, task.id, {"action": "add", "step": new_seq, "description": desc})
                return ToolResult(
                    ok=True,
                    output={"action": "add", "step": new_seq, "description": desc},
                    display=f"added step {new_seq}: {desc}")

            elif action == "remove":
                if step_num is None:
                    return ToolResult(ok=False, display="need step",
                                      error="step number is required for remove")
                step_num = int(step_num)
                await conn.execute(
                    "DELETE FROM task_step WHERE task_id = $1 AND seq = $2",
                    task.id, step_num)
                # Renumber remaining steps to fill the gap
                await conn.execute(
                    "UPDATE task_step SET seq = seq - 1 WHERE task_id = $1 AND seq > $2",
                    task.id, step_num)
                with contextlib.suppress(Exception):
                    await _folder(ctx, "log_event", task.id, "task_modified",
                                 {"action": "remove", "step": step_num})
                await _emit_task_modified(conn, ctx, task.id, {"action": "remove", "step": step_num})
                return ToolResult(
                    ok=True,
                    output={"action": "remove", "step": step_num},
                    display=f"removed step {step_num}")

            elif action == "replace":
                if step_num is None or not desc:
                    return ToolResult(ok=False, display="need step + description",
                                      error="step and description are required for replace")
                step_num = int(step_num)
                await conn.execute(
                    "UPDATE task_step SET description = $3, status = 'pending', "
                    "  attempts = 0, last_error = NULL, failure_class = NULL, "
                    "  checkpoint = NULL "
                    "WHERE task_id = $1 AND seq = $2",
                    task.id, step_num, desc)
                with contextlib.suppress(Exception):
                    await _folder(ctx, "log_event", task.id, "task_modified",
                                 {"action": "replace", "step": step_num, "description": desc})
                await _emit_task_modified(conn, ctx, task.id, {"action": "replace", "step": step_num, "description": desc})
                return ToolResult(
                    ok=True,
                    output={"action": "replace", "step": step_num, "description": desc},
                    display=f"replaced step {step_num}: {desc}")

            elif action == "reset":
                if step_num is None:
                    return ToolResult(ok=False, display="need step",
                                      error="step number is required for reset")
                step_num = int(step_num)
                await conn.execute(
                    "UPDATE task_step SET status = 'pending', attempts = 0, "
                    "  last_error = NULL, failure_class = NULL, checkpoint = NULL, "
                    "  verified = false, verified_by = NULL "
                    "WHERE task_id = $1 AND seq = $2",
                    task.id, step_num)
                with contextlib.suppress(Exception):
                    await _folder(ctx, "log_event", task.id, "task_modified",
                                 {"action": "reset", "step": step_num})
                await _emit_task_modified(conn, ctx, task.id, {"action": "reset", "step": step_num})
                return ToolResult(
                    ok=True,
                    output={"action": "reset", "step": step_num},
                    display=f"reset step {step_num} to pending")

            elif action == "edit_step":
                if step_num is None or not desc:
                    return ToolResult(ok=False, display="need step + description",
                                      error="step and description are required for edit_step")
                step_num = int(step_num)
                res = await conn.execute(
                    "UPDATE task_step SET description = $3 "
                    "WHERE task_id = $1 AND seq = $2",
                    task.id, step_num, desc)
                if res == "UPDATE 0":
                    return ToolResult(ok=False, display=f"no step {step_num}",
                                      error=f"step {step_num} does not exist on this task")
                with contextlib.suppress(Exception):
                    await _folder(ctx, "log_event", task.id, "task_modified",
                                 {"action": "edit_step", "step": step_num,
                                  "description": desc})
                await _emit_task_modified(conn, ctx, task.id, {"action": "edit_step", "step": step_num, "description": desc})
                return ToolResult(
                    ok=True,
                    output={"action": "edit_step", "step": step_num, "description": desc},
                    display=f"edited step {step_num}: {desc}")

            elif action == "change_objective":
                if not desc:
                    return ToolResult(ok=False, display="need new objective",
                                      error="description is required for change_objective (the new title)")
                pass   # nothing to do inside this txn - change_objective runs outside
            else:
                return ToolResult(ok=False, display="bad action",
                                  error=f"unknown action '{action}' — use add/remove/replace/reset/edit_step/change_objective")
        # change_objective runs OUTSIDE the step-txn above because store.rename_objective takes
        # its own connection (and would deadlock on the task row).
        if action == "change_objective":
            await ctx.tasks.rename_objective(task.id, desc)
            return ToolResult(
                ok=True,
                output={"action": "change_objective", "objective": desc},
                display=f"renamed task to: {desc}")


def register_builtins(registry: ToolRegistry) -> None:
    for tool in (PlanTask(), AdvanceTask(), FinishTask(), ReviewTask(), ConfirmTask(), ModifyTask()):
        registry.register(tool)
