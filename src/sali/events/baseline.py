"""Persistent machine baseline (spec §18/§19).

The learned "normal" for the machine, surviving restarts. `reconcile` folds the current set of items
(e.g. listening ports) into the baseline and returns the ones that are genuinely NEW — not present in
the baseline before — so a deviation is recognised even if it appeared while Sali was offline. The very
first observation of a kind is treated as baseline ESTABLISHMENT (nothing is "new" when we've never
looked), so first boot doesn't flag the whole machine as anomalous. Deterministic; no model.
"""

from __future__ import annotations

from typing import Any


class Baseline:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def reconcile(self, kind: str, current: set[str]) -> set[str]:
        """Update the baseline with `current` and return the items that are NEW for this kind. Empty on
        first establishment (an empty baseline → everything is just the starting normal)."""
        async with self._pool.acquire() as conn, conn.transaction():
            known = {r["item"] for r in await conn.fetch(
                "SELECT item FROM machine_baseline WHERE kind=$1", kind)}
            establishing = not known
            if current:
                await conn.executemany(
                    "INSERT INTO machine_baseline (kind, item) VALUES ($1,$2) "
                    "ON CONFLICT (kind, item) DO UPDATE SET observations=machine_baseline.observations+1, "
                    "  last_seen=now()",
                    [(kind, item) for item in current])
        return set() if establishing else (current - known)

    async def known(self, kind: str) -> set[str]:
        async with self._pool.acquire() as conn:
            return {r["item"] for r in await conn.fetch(
                "SELECT item FROM machine_baseline WHERE kind=$1", kind)}
