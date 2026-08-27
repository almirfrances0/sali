"""WebServices — the read facades a browser needs, assembled once at the composition edge.

The web is a *second interface* to the same Sali: every facade here is the SAME one the runtime uses
(GraphService, MemoryService, SelfStateStore, WorldStateBuilder, TaskStore, ScheduleStore,
HealthService) plus the EventHub bridge. No logic is duplicated — routes only serialize what these
already return. `presence()` DERIVES the rich ONLINE/EXECUTING/THINKING/… state the vision wants from
real runtime state (a live run's FSM state, else self-state mode, else recent perception) — there is no
such enum stored, so it is computed, never invented.
"""

from __future__ import annotations

from typing import Any

from sali.api.stream import EventHub
from sali.core.clock import SystemClock
from sali.graph.service import GraphService
from sali.memory.service import MemoryService
from sali.perception.service import build_perception
from sali.runtime.health import HealthService
from sali.runtime.self_state import SelfStateStore
from sali.runtime.world_state import WorldStateBuilder
from sali.scheduler.store import ScheduleStore
from sali.tasks.store import TaskStore
from sali.tools.registry import ToolRegistry, default_registry

# A live run's FSM state → the presence word a person understands. Anything not listed (RETRIEVE,
# BUILD_CONTEXT, REASON_PLAN, UNDERSTAND, RESPOND, …) reads as "thinking".
_RUN_STATE_PRESENCE = {
    "execute_tool": "executing", "observe": "executing", "verify": "executing",
    "tool_denied": "executing", "await_confirm": "waiting", "learn": "learning",
}
_TIERS = ("critical", "important", "interesting")


class WebServices:
    """Built once per process by ``create``; held on ``app.state`` and read by every route."""

    def __init__(
        self, *, kernel: Any, pool: Any, graph: GraphService, memory: MemoryService,
        self_state: SelfStateStore, world: WorldStateBuilder, tasks: TaskStore,
        schedules: ScheduleStore, health: HealthService, hub: EventHub,
        registry: ToolRegistry,
    ) -> None:
        self.kernel = kernel
        self.settings = kernel.settings
        self.provider = kernel.provider
        self.pool = pool
        self.graph = graph
        self.memory = memory
        self.self_state = self_state
        self.world = world
        self.tasks = tasks
        self.schedules = schedules
        self.health = health
        self.hub = hub
        self.registry = registry

    @classmethod
    async def create(cls, kernel: Any) -> WebServices:
        pool = await kernel.pool()
        perception = build_perception(kernel.settings)
        hub = EventHub(pool)
        await hub.start()
        return cls(
            kernel=kernel, pool=pool,
            graph=GraphService(pool),
            memory=MemoryService(pool, kernel.provider),
            self_state=SelfStateStore(pool),
            world=WorldStateBuilder(pool, perception),
            tasks=TaskStore(pool),
            schedules=ScheduleStore(pool, SystemClock()),
            health=HealthService(pool, kernel.provider, perception=perception),
            hub=hub,
            registry=default_registry(),
        )

    async def stop(self) -> None:
        await self.hub.stop()

    async def presence(self) -> dict[str, Any]:
        """What Sali is doing RIGHT NOW, derived from real state (never a stored flag)."""
        async with self.pool.acquire() as conn:
            run = await conn.fetchrow(
                "SELECT run_id, state FROM agent_runs WHERE status='running' "
                "ORDER BY started_at DESC LIMIT 1")
            mode = await conn.fetchval("SELECT mode FROM sali_state WHERE id") or "idle"
            observing = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM event WHERE event_type='desktop.observed' "
                "AND created_at > now() - interval '120 seconds')")
        if run is not None:
            presence = _RUN_STATE_PRESENCE.get(str(run["state"]), "thinking")
            return {"presence": presence, "mode": mode, "active_run": str(run["run_id"]),
                    "run_state": str(run["state"])}
        if mode == "working":
            presence = "thinking"
        elif mode == "learning":
            presence = "learning"
        else:
            presence = "observing" if observing else "idle"
        return {"presence": presence, "mode": mode, "active_run": None, "run_state": None}

    async def attention_counts(self, *, hours: int = 24) -> dict[str, Any]:
        """Attention tier counts from real surfaced observations. ROUTINE is intentionally not persisted
        (dropped at the sink as IGNORE), so it is reported as null — honest, not a fabricated number."""
        hours = max(1, min(hours, 720))
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT payload->>'tier' AS tier, count(*) AS n FROM event "
                "WHERE event_type='desktop.observed' "
                f"AND created_at > now() - interval '{hours} hours' GROUP BY 1")
        by_tier = {str(r["tier"]): int(r["n"]) for r in rows if r["tier"]}
        counts: dict[str, Any] = {t: by_tier.get(t, 0) for t in _TIERS}
        counts["routine"] = None  # never persisted — see docstring
        counts["window_hours"] = hours
        return counts
