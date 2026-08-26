"""DbObservationSink — persist surfaced observations to the durable event log (sali3 §7 fusion).

This is what makes the continuous engine actually reach Sali: each meaningful observation becomes a
`desktop.observed` event, which the agent loop reads on its next turn (twin.awareness) so Sali KNOWS
what Almir has been doing — files he edited, apps he switched to — instead of the stream dead-ending
in a log. Only observations above an importance threshold are kept, so routine low-signal churn
never floods the log (§41 filter-first, again, at the persistence boundary).
"""

from __future__ import annotations

from typing import Any

from sali.events.base import Observation
from sali.obs.log import get_logger

log = get_logger("sali.events.sink")


class DbObservationSink:
    def __init__(self, pool: Any, *, min_importance: float = 0.6) -> None:
        self._pool = pool
        self._min = min_importance

    async def observe(self, obs: Observation) -> None:
        if obs.importance < self._min:
            return  # keep only what's worth Sali noticing; the rest is already aggregated + logged
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO event (event_type, subject_type, payload) "
                    "VALUES ('desktop.observed', 'desktop', $1)",
                    obs.to_dict(),
                )
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort; never kill perception
            log.warning("could not persist observation: %s", exc)
