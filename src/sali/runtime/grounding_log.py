"""Sali's self-claim contradiction ledger + grounding metrics (§32/§44/§45).

The response-claim validator (`verify/response_claims`) is Sali's last gate before a reply reaches
Almir: it strikes any sentence that asserts something false about his own action, state, capability,
or a file he didn't send. That correction used to vanish with the turn — journalled and emitted as an
event, but never durable, so "how often does Sali over-claim, and is it improving?" had no answer.

`GroundingLog` closes that: every struck claim is one durable row. `record()` writes them at the
moment of the strike; `metrics()` sums them (total, by family, last 24h) for the grounding sink; and
`recent()` lists the actual sentences so a human can review what Sali tried to say and what it became.
This is a MEASUREMENT surface, not a gate — the strike already happened; here we remember it.
"""

from __future__ import annotations

from typing import Any


class GroundingLog:
    """Durable record of self-claims the validator struck, plus the metrics derived from them."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def record(self, *, session_id: Any, run_id: Any, claims: list[Any]) -> int:
        """Persist one row per struck claim. `claims` are ResponseClaim objects (kind/sentence/detail/
        verdict). Returns how many rows were written. Best-effort by contract of the caller — but this
        method itself does not swallow, so a real schema problem surfaces in tests."""
        rows = [c for c in (claims or []) if getattr(c, "sentence", None)]
        if not rows:
            return 0
        async with self._pool.acquire() as conn:
            for c in rows:
                await conn.execute(
                    "INSERT INTO grounding_event (session_id, run_id, kind, verdict, sentence, detail) "
                    "VALUES ($1,$2,$3,$4,$5,$6::jsonb)",
                    session_id, run_id,
                    (getattr(c, "kind", None) or "unknown")[:40],
                    (getattr(c, "verdict", None) or "unsupported")[:40],
                    (getattr(c, "sentence", "") or "")[:1000],
                    _detail_json(getattr(c, "detail", None)))
        return len(rows)

    async def metrics(self) -> dict[str, Any]:
        """Aggregate the ledger for the grounding sink: lifetime total, by family, and a 24h window —
        the numbers that say whether Sali's over-claiming is rare and getting rarer."""
        async with self._pool.acquire() as conn:
            # 'flagged' rows are the flag-only cross-turn consistency detector — a DIFFERENT thing from a
            # STRUCK self-claim, so they are counted separately and never inflate the strike total.
            total = int(await conn.fetchval(
                "SELECT count(*) FROM grounding_event WHERE verdict <> 'flagged'") or 0)
            last_24h = int(await conn.fetchval(
                "SELECT count(*) FROM grounding_event WHERE verdict <> 'flagged' "
                "AND at > now() - interval '24 hours'") or 0)
            last_at = await conn.fetchval(
                "SELECT max(at) FROM grounding_event WHERE verdict <> 'flagged'")
            by_kind = {r["kind"]: int(r["n"]) for r in await conn.fetch(
                "SELECT kind, count(*) AS n FROM grounding_event WHERE verdict <> 'flagged' "
                "GROUP BY kind ORDER BY n DESC")}
            cross_turn = int(await conn.fetchval(
                "SELECT count(*) FROM grounding_event WHERE kind = 'cross_turn'") or 0)
        return {
            "self_claims_struck_total": total,
            "self_claims_struck_24h": last_24h,
            "by_family": by_kind,
            "last_struck_at": last_at.isoformat() if last_at else None,
            "cross_turn_flagged_total": cross_turn,
        }

    async def recent(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """The most recently struck self-claims — the actual sentences, for review (§44)."""
        limit = max(1, min(int(limit or 20), 200))
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT at, kind, verdict, sentence, detail FROM grounding_event "
                "ORDER BY at DESC LIMIT $1", limit)
        return [{
            "at": r["at"].isoformat() if r["at"] else None,
            "family": r["kind"],
            "verdict": r["verdict"],
            "sentence": r["sentence"],
            "detail": r["detail"],
        } for r in rows]


def _detail_json(detail: Any) -> str:
    """Coerce a claim's detail into a JSON object string for the jsonb column."""
    import json
    if detail is None:
        return "{}"
    if isinstance(detail, str):
        return json.dumps({"reason": detail[:500]})
    try:
        return json.dumps(detail)
    except Exception:  # noqa: BLE001 - detail is diagnostic; never let it break the record
        return json.dumps({"reason": str(detail)[:500]})


__all__ = ["GroundingLog"]
