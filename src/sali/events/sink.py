"""DbObservationSink — persist ATTENTION-worthy observations to the durable event log (§7/§14 fusion).

This is what makes the continuous engine actually reach Sali: each observation the Attention Engine
judges worth keeping becomes a `desktop.observed` event carrying its tier + action, which the agent
loop reads on its next turn (twin.awareness / world-state) so Sali KNOWS what Almir has been doing —
and the proactive loop (§16) can later act on the ones tagged `notify`/`investigate`. Attention drops
routine churn deterministically, so low-signal noise never floods the log (§41 filter-first).
"""

from __future__ import annotations

import os
from typing import Any

from sali.events import attention
from sali.events.attention import AttentionAction
from sali.events.base import EventKind, Observation
from sali.events.importance import _SOURCE_SUFFIXES
from sali.obs.log import get_logger

log = get_logger("sali.events.sink")

_CONFIG_FILES = frozenset({
    ".env", "dockerfile", "docker-compose.yml", "docker-compose.yaml", "pyproject.toml",
    "makefile", "nginx.conf", "requirements.txt", "package.json", "cargo.toml",
})
# System-event kinds map directly to their attention signal.
_KIND_SIGNAL = {
    EventKind.PORT_OPENED: "new_service",
    EventKind.SERVICE_FAILED: "service_failed",
    EventKind.DISK_PRESSURE: "disk_full",
}


def _signals_for(obs: Observation) -> frozenset[str]:
    """Categorical attention signals derived from an observation — filesystem, window, or system."""
    if obs.kind in _KIND_SIGNAL:
        return frozenset({_KIND_SIGNAL[obs.kind]})
    sample = str(obs.detail.get("sample", ""))
    name = sample.rsplit(os.sep, 1)[-1].lower()
    signals: set[str] = set()
    if obs.kind is EventKind.FILE_DELETED and name.endswith(_SOURCE_SUFFIXES):
        signals.add("source_delete")           # deleting source is more notable than editing it
    if name in _CONFIG_FILES or name.endswith(".conf"):
        signals.add("config_change")           # a changed project/service config is worth attention
    return frozenset(signals)


class DbObservationSink:
    def __init__(self, pool: Any, *, min_importance: float | None = None) -> None:
        self._pool = pool
        self._min = min_importance  # optional hard floor; attention is the real gate

    async def observe(self, obs: Observation) -> None:
        if self._min is not None and obs.importance < self._min:
            return
        verdict = attention.assess(
            importance=obs.importance, signals=_signals_for(obs), count=obs.count)
        if verdict.action is AttentionAction.IGNORE:
            return  # routine churn — attention says it's not worth Sali's notice
        payload = obs.to_dict()
        payload["tier"] = verdict.tier.value
        payload["action"] = verdict.action.value
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO event (event_type, subject_type, payload) "
                    "VALUES ('desktop.observed', 'desktop', $1)",
                    payload,
                )
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort; never kill perception
            log.warning("could not persist observation: %s", exc)
