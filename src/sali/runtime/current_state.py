"""Sali's CURRENT-STATE snapshot (§36-38): one authoritative read of "what is true right now".

Everything here already lives in a store somewhere — identity in the graph, mode/focus/outcome in
sali_state, promises in the commitment table, over-claims in the grounding ledger, the transport in the
live connection. What was missing is a single call that gathers them AND tags each fact with the
authority tier it came from (see runtime/claim_authority), so a reader — a human on the iPhone, or Sali
consulting himself — sees not just the value but how much to trust it, and by what precedence a
conflict would be settled. This composes the existing stores; it owns no new state and writes nothing.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from typing import Any

from sali.runtime.claim_authority import ORDER, PRECEDENCE_LINE
from sali.runtime.claim_authority import ClaimAuthority as A


def _fact(tier: A, value: Any, source: str) -> dict[str, Any]:
    """One tagged fact: its value, the authority tier it came from, and the concrete source."""
    return {"value": value, "authority": tier.label, "source": source}


def _stale(at: Any, *, seconds: int = 3600) -> bool | None:
    """True when a timestamp is older than `seconds` (default 1h) — so a reader can tell "this just
    happened" from "this happened an hour ago"; an old outcome read as current is its own hallucination.
    None when there's no timestamp to judge."""
    if at is None:
        return None
    try:
        return (datetime.now(timezone.utc) - at).total_seconds() > seconds
    except Exception:  # noqa: BLE001 - staleness is advisory; never break the snapshot
        return None


class CurrentState:
    """Compose the authoritative right-now snapshot from the stores that already own each piece."""

    def __init__(self, pool: Any, *, connection: Any = None) -> None:
        self._pool = pool
        self._connection = connection   # the live ConnectionContext (OBSERVED), if the runtime has one

    async def snapshot(self) -> dict[str, Any]:
        from sali.runtime.self_state import SelfStateStore

        view = await SelfStateStore(self._pool).assemble()
        env = view.get("environment") or {}
        facts: dict[str, Any] = {}

        # WHO — identity, from durable structured state (the agent:sali node), not a guess.
        facts["identity"] = _fact(A.STATE, {
            "name": view.get("identity"),
            "presentation": view.get("presentation"),
            "pronouns": view.get("pronouns"),
            "role": view.get("role"),
            "owner": view.get("owner"),
            "preferred_address": view.get("preferred_address"),
        }, "agent:sali graph node")

        # WHERE / WITH WHAT — machine + model + workspace, from the agent subgraph.
        facts["environment"] = _fact(A.STATE, {
            k: env.get(k) for k in ("machine", "os", "model", "workspace") if env.get(k)
        }, "agent subgraph")

        # HOW ALMIR IS CONNECTED — the REAL transport this session is on: observed, not remembered.
        if self._connection is not None:
            with contextlib.suppress(Exception):
                desc = self._connection.describe()
                if desc:
                    facts["connection"] = _fact(A.OBSERVED, desc, "live transport")

        # WHAT I'M DOING — mode, focus, current task, all from sali_state / the task table.
        facts["mode"] = _fact(A.STATE, view.get("mode"), "sali_state")
        if view.get("current_focus"):
            facts["current_focus"] = _fact(A.STATE, view.get("current_focus"), "sali_state")
        if view.get("current_task"):
            facts["current_task"] = _fact(A.STATE, view.get("current_task"), "task table")

        # HOW THE LAST TURN WENT — the plainly-recorded success/failure (§11), each with its timestamp.
        if view.get("last_failure"):
            _fa = view.get("last_failure_at")
            facts["last_failure"] = _fact(A.STATE, {
                "summary": view.get("last_failure"), "at": _fa, "stale": _stale(_fa)}, "sali_state")
        if view.get("last_success"):
            _sa = view.get("last_success_at")
            facts["last_success"] = _fact(A.STATE, {
                "summary": view.get("last_success"), "at": _sa, "stale": _stale(_sa)}, "sali_state")

        # WHAT I OWE — open commitments and the next one due, from the commitment FSM.
        with contextlib.suppress(Exception):
            from sali.tasks.commitments import CommitmentStore

            open_c = await CommitmentStore(self._pool).open(limit=5)
            nxt = None
            if open_c:
                dl = open_c[0].get("deadline")
                nxt = {"description": open_c[0].get("description"),
                       "deadline": dl.isoformat() if dl else None}
            facts["open_commitments"] = _fact(
                A.STATE, {"count": len(open_c), "next": nxt}, "commitment table")

        # HOW GROUNDED I'VE BEEN — the self-claim ledger metrics (§45): over-claims caught before Almir.
        with contextlib.suppress(Exception):
            from sali.runtime.grounding_log import GroundingLog

            facts["grounding"] = _fact(
                A.STATE, await GroundingLog(self._pool).metrics(), "grounding_event ledger")

        # WHAT I'M UNSURE OF — the needs_grounding memories, surfaced as open questions.
        if view.get("uncertainties"):
            facts["uncertainties"] = _fact(
                A.STATE, view.get("uncertainties"), "needs_grounding memories")

        as_of = None
        with contextlib.suppress(Exception):
            async with self._pool.acquire() as conn:
                ts = await conn.fetchval("SELECT now()")
                as_of = ts.isoformat() if ts else None

        return {
            "as_of": as_of,
            "authority_order": [a.label for a in ORDER],   # highest first
            "precedence": PRECEDENCE_LINE,
            "facts": facts,
        }


__all__ = ["CurrentState"]
