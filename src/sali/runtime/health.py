"""Subsystem health + online/offline awareness (spec §51/§52/§53).

Sali should know whether its own parts are working — its model, datastore, embedder, perception — and
whether the internet is reachable, so it degrades honestly ("my perception subsystem is unavailable")
and never claims to have researched something while offline (§53). Each probe is cheap and defensive:
a subsystem that can't be reached reads as down, never as an exception.

Connectivity is checked privacy-minimally (§64): first the purely-local default-route check (no packets
at all); only if a route exists is a bare TCP connection opened to a public DNS resolver — a SYN and
nothing more, no data sent — to confirm real reachability.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sali.core.net import check_internet, has_default_route
from sali.obs.log import get_logger

log = get_logger("sali.runtime.health")

__all__ = ["Health", "HealthService", "check_internet", "has_default_route"]


@dataclass(slots=True)
class Health:
    datastore: bool
    model: bool
    embedder: bool
    perception: bool
    internet: bool
    detail: dict[str, str] = field(default_factory=dict)

    @property
    def subsystems(self) -> dict[str, bool]:
        return {"datastore": self.datastore, "model": self.model, "embedder": self.embedder,
                "perception": self.perception, "internet": self.internet}

    @property
    def degraded(self) -> list[str]:
        return [name for name, ok in self.subsystems.items() if not ok]

    @property
    def all_ok(self) -> bool:
        return not self.degraded

    def render(self) -> str:
        if self.all_ok:
            return "All my subsystems are healthy" + ("" if self.internet else " (offline)")
        return "Degraded: " + ", ".join(self.degraded)


class HealthService:
    """Live health of Sali's own subsystems. Each check is best-effort and independent."""

    def __init__(self, pool: Any, provider: Any, *, perception: Any = None) -> None:
        self._pool = pool
        self._provider = provider
        self._perception = perception

    async def online(self) -> bool:
        return await check_internet()

    async def check(self, *, probe_embedder: bool = True) -> Health:
        detail: dict[str, str] = {}
        datastore = await self._probe(self._probe_datastore, detail, "datastore")
        model = await self._probe(self._provider.health, detail, "model")
        embedder = model
        if probe_embedder and model:
            embedder = await self._probe(self._probe_embedder, detail, "embedder")
        perception = await self._probe(self._probe_perception, detail, "perception")
        internet = await check_internet()
        detail["internet"] = "reachable" if internet else "offline"
        return Health(datastore=datastore, model=model, embedder=embedder,
                      perception=perception, internet=internet, detail=detail)

    async def _probe(self, fn: Any, detail: dict[str, str], name: str) -> bool:
        try:
            ok = bool(await fn())
            detail[name] = "ok" if ok else "unavailable"
            return ok
        except Exception as exc:  # noqa: BLE001 - a failed probe means down, never an exception
            detail[name] = f"error: {str(exc)[:60]}"
            return False

    async def _probe_datastore(self) -> bool:
        async with self._pool.acquire() as conn:
            return bool(await conn.fetchval("SELECT 1"))

    async def _probe_embedder(self) -> bool:
        vecs = await self._provider.embed(["ping"])
        return bool(vecs and vecs[0])

    async def _probe_perception(self) -> bool:
        if self._perception is None:
            return False
        snap = await self._perception.snapshot(ui=False)
        return bool((snap or {}).get("available"))
