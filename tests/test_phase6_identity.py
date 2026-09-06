"""PHASE 6 — reliability matrix · identity & self-model.

The epistemic authority hierarchy (claim_authority), the stale-focus guard (self_state._is_fresh), and
the about_note "what you already know about Almir" digest filter had ZERO permanent coverage before
Phase 6. This locks the pure logic deterministically and proves the about_note query upholds the
canonical current-row invariant (harden regression [H]: superseded_by IS NULL + layer<>'identity').
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sali.runtime.claim_authority import ORDER, PRECEDENCE_LINE, ClaimAuthority, resolve
from sali.runtime.self_state import _is_fresh


# ---- authority hierarchy (pure) ------------------------------------------------------------------

def test_observed_beats_recall() -> None:
    got = resolve([(ClaimAuthority.RECALL, "mysql (from memory)"),
                   (ClaimAuthority.OBSERVED, "mysql (probed this turn)")])
    assert got == (ClaimAuthority.OBSERVED, "mysql (probed this turn)")


def test_resolve_skips_none_and_state_beats_recall() -> None:
    got = resolve([(ClaimAuthority.OBSERVED, None), (ClaimAuthority.RECALL, "she/her"),
                   (ClaimAuthority.STATE, "male")])
    assert got == (ClaimAuthority.STATE, "male")


def test_resolve_empty_is_none() -> None:
    assert resolve([]) is None and resolve([(ClaimAuthority.BELIEF, None)]) is None


def test_order_is_strictly_descending() -> None:
    assert ORDER == (ClaimAuthority.OBSERVED, ClaimAuthority.STATE,
                     ClaimAuthority.RECALL, ClaimAuthority.BELIEF)
    assert [a.value for a in ORDER] == sorted((a.value for a in ORDER), reverse=True)
    assert "observe" in PRECEDENCE_LINE and "guess" in PRECEDENCE_LINE


# ---- stale-focus guard (pure) --------------------------------------------------------------------

def test_is_fresh_edge_paths() -> None:
    now = datetime.now(timezone.utc)
    assert _is_fresh(None) is False                       # no basis -> suppress
    assert _is_fresh(now - timedelta(hours=2)) is False   # stale working focus -> suppress
    assert _is_fresh(now - timedelta(seconds=30)) is True # genuinely current focus -> surface


# ---- about_note digest filter (rolled-back DB) ---------------------------------------------------

_ABOUT_SQL = (
    "SELECT content FROM memory WHERE source='user_explicit' "
    "AND valid_until IS NULL AND superseded_by IS NULL AND layer <> 'identity' "
    "AND content NOT ILIKE 'I am Sali%' "
    "AND coalesce(confidence, 1) >= 0.6 "
    "ORDER BY importance DESC NULLS LAST, updated_at DESC LIMIT 6")


async def test_about_note_filter_excludes_superseded_identity_and_lowconf(db_conn: Any) -> None:
    # Isolate: this runs inside a rolled-back transaction, so clearing the table is safe and discarded.
    await db_conn.execute("DELETE FROM memory")
    valid_id = await db_conn.fetchval(
        "INSERT INTO memory (layer, content, source, confidence, importance) "
        "VALUES ('semantic','Almir''s wife is Faith','user_explicit',0.9,0.95) RETURNING id")
    # (a) superseded but valid_until still NULL — the exact naive-restore hazard the invariant guards
    await db_conn.execute(
        "INSERT INTO memory (layer, content, source, confidence, importance, superseded_by) "
        "VALUES ('semantic','Almir uses vim (SUPERSEDED)','user_explicit',0.9,0.99,$1)", valid_id)
    # (b) an identity-layer Sali-directed rule — a fact ABOUT Sali, not about Almir
    await db_conn.execute(
        "INSERT INTO memory (layer, content, source, confidence, importance) "
        "VALUES ('identity','You are Sali, my assistant','user_explicit',0.9,0.99)")
    # (c) the first-person Sali seed
    await db_conn.execute(
        "INSERT INTO memory (layer, content, source, confidence, importance) "
        "VALUES ('semantic','I am Sali, Almir''s companion','user_explicit',0.9,0.99)")
    # (d) a low-confidence guess
    await db_conn.execute(
        "INSERT INTO memory (layer, content, source, confidence, importance) "
        "VALUES ('semantic','Almir might like jazz','user_explicit',0.4,0.99)")

    rows = [r["content"] for r in await db_conn.fetch(_ABOUT_SQL)]
    assert rows == ["Almir's wife is Faith"]  # only the current, high-confidence, non-identity fact
