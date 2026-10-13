"""No new faculty may be built, tested, and left unwired.

THE CHARACTERISTIC FAILURE OF THIS CODEBASE, in one test.

Sali's problem has never been missing features. It is mechanisms that were written, committed,
described in a comment as working, and never called. Found by hand in a single week:

  - THREAD / CORRECTED / STOPPED were written into the continuation packet on EVERY compaction and
    never rendered back. The file's own comment said "the order here is the order Sali reads them
    back in" — describing behaviour the code did not have.
  - `sali.person` — a table with relationship_type, preferences, context — held zero rows, ever.
  - `sali.graph_node` held 3,187 ext_tool nodes and ZERO people, including its owner.
  - `message.token_count` was written on every row since the table existed and never read.
  - `consumeFocusedTask()` in the iOS app was defined and never called, so a push about a task
    dead-ended on an unfiltered list.
  - An origin-stamping change raised NameError on EVERY capture; a `contextlib.suppress` swallowed
    it, and durable memory would simply have stopped accruing, silently.

A unit test is not evidence a faculty is ALIVE. Thirty-four functions in this codebase have passing
tests and no caller — the test proves the code works while nothing runs it, which is worse than no
test at all, because it converts an absence into false confidence.

So: this test fails when a function is defined, undecorated, and referenced nowhere in `src/` except
its own definition — unless it is listed below WITH A REASON. The list is the point. It is a written
record of what is knowingly dark, and it can only shrink honestly: wiring something up requires
removing its line, and the test fails if a listed name becomes reachable but stays listed.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "sali"

# Called by name from outside Python's view — frameworks, protocols, duck-typed interfaces.
_PROTOCOL = frozenset({
    "run", "verify", "main", "setup", "teardown", "handle", "close", "start", "stop",
    "snapshot", "render", "describe", "to_options", "to_json", "to_db_payload", "execute",
})

# KNOWN DARK. Each line is a debt with a reason, not a permission slip.
#   wired-later  — deliberate: built ahead of the thing that will call it
#   api-surface  — reachable by an operator/tool/route indirectly, not from src
#   redundant    — superseded; candidate for deletion
_KNOWN_DARK: dict[str, str] = {
    # ── behaviour that is MISSING because of this, not merely unused ───────────────────────────────
    "fulfill": "commitments can be created but never closed — they accumulate forever (wired-later)",
    "should_back_off": "no back-off on proactive messages; nothing rate-limits initiative (wired-later)",
    "should_follow_up": "he never returns to a question he asked (wired-later)",
    "record_follow_up": "pairs with should_follow_up (wired-later)",
    "update_progress": "goal progress never advances (wired-later)",
    "subgoals": "goal decomposition never read (wired-later)",
    "backoff_seconds": "retry backoff never applied — retries are unspaced (wired-later)",
    "gap_analysis": "capability gaps never computed (wired-later)",
    "resume_pending": "a cleanup that crashed never resumes (wired-later)",
    "recover_attention": "attention recovery after a crash (wired-later)",
    "suspend_primary": "primary-task suspension (wired-later)",
    "has_standing_authorization": "standing consent never consulted (wired-later)",
    "grant_standing": "pairs with has_standing_authorization (wired-later)",
    "revoke_standing": "pairs with has_standing_authorization (wired-later)",
    "await_external": "external-dependency waiting (wired-later)",
    "require_verification": "external verification requirement (wired-later)",
    "set_verification": "external verification state (wired-later)",
    "set_credential_configured": "external credential state (wired-later)",
    "open_or_create": "conversation-thread opening (wired-later)",
    "looks_like_secret": "the secret GATE is unused; `redact`/`redact_obj` do the scrubbing and ARE wired",
    "set_dependencies": "skill/capability dependency graph (wired-later)",
    "is_autonomous": "authority check (wired-later)",
    "count_pending": "learning-queue depth (api-surface)",
    "get_health_summary": "watchdog health (api-surface)",
    "get_execution_history": "task execution history (api-surface)",
    "has_completed_execution": "execution lookup (api-surface)",
    "get_by_id": "external-entity lookup (api-surface)",
    "tools_with_capability": "capability→tool lookup (api-surface)",
    "advertised_capabilities": "adapter capability list (api-surface)",
    "path_is_real_file": "probe helper (api-surface)",
    "os_package_key": "tool vocabulary helper (api-surface)",
    "is_known_capability": "tool vocabulary helper (api-surface)",
    "render_task_header": "packet rendering helper (redundant — render_packet inlines it)",
    "_over_context": "superseded by the deferred-compaction path (redundant)",
    # ── CLI rendering helpers orphaned by renderer rewrite ─────────────────────────────────────────
    "_context_window": "CLI context window display — orphaned from renderer rewrite (redundant)",
    "_fmt_tool": "CLI tool formatter — orphaned from renderer rewrite (redundant)",
    "_stream_turn": "CLI turn streaming — orphaned from renderer rewrite (redundant)",
    "print_divider": "CLI divider — orphaned from renderer rewrite (redundant)",
    "print_session_bar": "CLI session bar — orphaned from renderer rewrite (redundant)",
    "render_task_progress": "CLI task progress — orphaned from renderer rewrite (redundant)",
    "render_user_input": "CLI user input display — orphaned from renderer rewrite (redundant)",
    "set_thinking": "CLI thinking indicator — orphaned from renderer rewrite (redundant)",
    "log_message": "CLI stderr silencer — orphaned from renderer rewrite (redundant)",
    # ── runtime loop helpers built for refactored paths ────────────────────────────────────────────
    "_self_directed_note": "runtime self-directed note — path was refactored away (redundant)",
    "_with_research_block": "runtime research block injection — path was refactored away (redundant)",
    "_with_workspace_block": "runtime workspace block injection — path was refactored away (redundant)",
    # ── store/service methods whose callers haven't been built ─────────────────────────────────────
    "has_passing_review": "reviewer pass check — caller not yet wired (wired-later)",
    "mark_checked": "obligation check-mark — caller not yet wired (wired-later)",
    "mark_testing": "behavior proposal testing — caller not yet wired (wired-later)",
    "verify_from_reviewer": "learning-candidate verification from reviewer — caller not yet wired (wired-later)",
    "verify_task_candidates": "task research candidate verification — caller not yet wired (wired-later)",
    "write_heartbeat": "task recovery heartbeat — caller not yet wired (wired-later)",
    "resolve_relative": "workspace relative-path resolution — caller not yet wired (wired-later)",
    "re_evaluate": "curiosity re-evaluation — caller not yet wired (wired-later)",
    "get_one": "memory admin single-id lookup (api-surface)",
    "is_bound": "cognitive voice binding check — voice module was refactored (redundant)",
    "set_operation": "self-state operation setter — caller not yet wired (wired-later)",
    "set_lease": "coordinator lease setter — caller not yet wired (wired-later)",
    "all_strategies": "strategy listing — api-surface, not called from src directly (api-surface)",
    # ── services/methods built ahead of their callers ──────────────────────────────────────────────
    "archive_completed_task": "task archival after completion — caller not yet wired (wired-later)",
    "archive_stale": "stale curiosity archival — caller not yet wired (wired-later)",
    "broadcast_notification": "WS notification broadcast — caller not yet wired (wired-later)",
    "broadcast_task_update": "WS task-update broadcast — caller not yet wired (wired-later)",
    "discover_tools": "twin tool discovery — caller not yet wired (wired-later)",
    "dismiss": "open-loop/initiative dismiss — caller not yet wired (wired-later)",
    "do_GET": "CLI HTTP GET handler — orphaned from CLI rewrite (redundant)",
    "for_object": "digital-object lookup — caller not yet wired (wired-later)",
}


def _undecorated_defs() -> dict[str, list[tuple[str, int]]]:
    """Every plain function/method. Decorated ones (routes, fixtures, properties, registered tools)
    are called by a framework and cannot be judged by reference counting."""
    out: dict[str, list[tuple[str, int]]] = {}
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in str(path) or "-bak" in path.name:
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:                      # a file mid-edit must not fail the whole suite
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if node.decorator_list or node.name.startswith("__"):
                continue
            out.setdefault(node.name, []).append((str(path.relative_to(SRC)), node.lineno))
    return out


def _source_blob() -> str:
    parts = []
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in str(path) or "-bak" in path.name:
            continue
        parts.append(path.read_text())
    return "\n".join(parts)


def _unwired() -> dict[str, list[tuple[str, int]]]:
    """Defined, and referenced nowhere in src/ except at its own definition.

    Counts BARE `\\bname\\b`, deliberately: a method is normally called as `obj.method()`, so a
    pattern that excludes the dot finds nothing but false positives. (That mistake was made while
    writing this and caught only because it flagged functions known to be called.)
    """
    defs, blob = _undecorated_defs(), _source_blob()
    dead = {}
    for name, sites in defs.items():
        if name in _PROTOCOL:
            continue
        if len(re.findall(rf"\b{re.escape(name)}\b", blob)) <= len(sites):
            dead[name] = sites
    return dead


def test_no_newly_dormant_faculty() -> None:
    """A faculty added and left unwired fails the suite, at the moment it is added."""
    unwired = _unwired()
    surprises = sorted(set(unwired) - set(_KNOWN_DARK))
    detail = "\n".join(
        f"  {n}  ({', '.join(f'{f}:{line}' for f, line in unwired[n])})" for n in surprises)
    assert not surprises, (
        "These are defined and called NOWHERE in src/ — built, possibly tested, and never wired:\n"
        f"{detail}\n\n"
        "Wire it to a real caller, delete it, or add it to _KNOWN_DARK with a reason. Do not add it "
        "to the list to make this pass: that list is the record of what Sali cannot actually do."
    )


def test_the_dark_list_does_not_rot() -> None:
    """A listed faculty that became reachable must leave the list.

    Without this the list only grows, and a debt that was quietly paid still reads as unpaid — which
    is the same false-confidence problem one level up.
    """
    unwired = _unwired()
    now_wired = sorted(set(_KNOWN_DARK) - set(unwired))
    assert not now_wired, (
        "These are listed as dark but are now called somewhere in src/. Remove them from "
        f"_KNOWN_DARK: {now_wired}"
    )


def test_the_reasons_are_real() -> None:
    """Every entry carries a reason. A bare name teaches the next reader nothing."""
    empty = sorted(n for n, why in _KNOWN_DARK.items() if len(why.strip()) < 15)
    assert not empty, f"_KNOWN_DARK entries need a real reason, not a placeholder: {empty}"
