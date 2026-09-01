"""ResourceIncidentStore (§21/§22/§23) — host-endangering events as durable operational experience.

A GPU OOM, VRAM/RAM pressure, a thermal warning, a full disk, or repeated model crashes are recorded as
durable incidents with what caused them, what was observed, and what mitigation worked. This becomes
NEGATIVE OPERATIONAL KNOWLEDGE: before a risky plan, Sali retrieves relevant prior incidents and adapts,
so it does not rediscover the same destructive mistake (§22/§23). Evidence-based — measured values, never
hallucinated fear (§16/§21).
"""

from __future__ import annotations

import contextlib
from typing import Any
from uuid import UUID, uuid4


class ResourceIncidentStore:
    def __init__(self, pool: Any, publisher: Any = None) -> None:
        self._pool = pool
        self._publisher = publisher

    async def record(
        self, *, kind: str, severity: str = "high", workload: str | None = None,
        observed: dict[str, Any] | None = None, task_id: UUID | None = None,
    ) -> UUID:
        iid = uuid4()
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO resource_incident (id, kind, severity, workload, observed, task_id) "
                "VALUES ($1,$2,$3,$4,$5,$6)", iid, kind, severity, workload, observed or {}, task_id)
        await self._emit("resource.incident_recorded",
                         {"incident_id": str(iid), "kind": kind, "severity": severity,
                          "workload": (workload or "")[:120]})
        return iid

    async def resolve(self, incident_id: UUID, *, mitigation: str) -> None:
        """Record what reduced the pressure — the reusable lesson (§21/§23)."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE resource_incident SET resolved=true, mitigation=$2, resolved_at=now() WHERE id=$1",
                incident_id, mitigation[:400])

    async def relevant(self, *, kind: str | None = None, workload: str | None = None,
                       limit: int = 5) -> list[dict[str, Any]]:
        """Prior incidents relevant to a proposed workload — retrieved during planning so the memory
        actually influences the next decision (§23). Keyword-matched on workload when given."""
        clause = "true"
        args: list[Any] = []
        if kind is not None:
            args.append(kind)
            clause += f" AND kind=${len(args)}"
        if workload is not None:
            args.append(f"%{workload[:60]}%")
            clause += f" AND coalesce(workload,'') ILIKE ${len(args)}"
        args.append(limit)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, kind, severity, workload, observed, mitigation, resolved, created_at "
                f"FROM resource_incident WHERE {clause} ORDER BY created_at DESC LIMIT ${len(args)}", *args)
        return [dict(r) for r in rows]

    async def counts(self) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT kind, count(*) AS n FROM resource_incident GROUP BY kind")
            openn = int(await conn.fetchval(
                "SELECT count(*) FROM resource_incident WHERE NOT resolved") or 0)
        return {"total": sum(int(r["n"]) for r in rows), "open": openn,
                "by_kind": {r["kind"]: int(r["n"]) for r in rows}}

    async def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        with contextlib.suppress(Exception):
            await self._publisher.emit(event_type=event_type, subject_type="resource",
                                       origin="runtime", data=data)
