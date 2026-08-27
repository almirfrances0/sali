"""The single composition root.

``Kernel`` wires the concretes (settings, logger, provider, and a lazily-created DB pool)
so the rest of the system depends on interfaces, not construction.
"""

from __future__ import annotations

from typing import Any

from sali.config.settings import Settings, load_settings
from sali.obs.log import configure_logging, get_logger
from sali.provider.base import ModelProvider
from sali.provider.registry import build_provider


class Kernel:
    def __init__(self, settings: Settings, provider: ModelProvider) -> None:
        self.settings = settings
        self.provider = provider
        self.log = get_logger("sali")
        self._pool: Any = None
        self._recovered = False  # crash-recovery runs ONCE per process, not per loop/WS connection

    @classmethod
    def create(cls, settings: Settings | None = None) -> Kernel:
        settings = settings or load_settings()
        configure_logging(settings.log_level)
        return cls(settings, build_provider(settings))

    async def pool(self) -> Any:
        if self._pool is None:
            from sali.db.pool import create_pool

            self._pool = await create_pool(self.settings)
        return self._pool

    async def agent_loop(self, *, confirmer: Any) -> Any:
        """Build the fully-wired agent loop. The single place the object graph is assembled."""
        from sali.context.engine import ContextEngine
        from sali.learning.service import LearningService
        from sali.retrieval.service import RetrievalService
        from sali.runtime.loop import AgentLoop
        from sali.security.policy import PolicyEngine
        from sali.tools.registry import default_registry

        pool = await self.pool()
        loop = AgentLoop(
            pool=pool,
            provider=self.provider,
            retrieval=RetrievalService(pool, self.provider),
            context=ContextEngine(self.provider, ctx_tokens=self.settings.model.ctx_default),
            registry=default_registry(),
            policy=PolicyEngine(),
            confirmer=confirmer,
            settings=self.settings,
            learning=LearningService(pool, self.provider),  # so memory actually ACCRUES (§17-19)
        )
        # Clean up any run a prior process left 'running' (hard crash / kill): mark it aborted so it
        # doesn't linger. Runs ONCE per process (not per WebSocket connection) and only over STALE
        # runs, so it can never touch a turn in flight — here or in another live process.
        if not self._recovered:
            self._recovered = True
            try:
                recovered = await loop.recover()
                if recovered:
                    self.log.info("recovered_orphan_runs", count=len(recovered))
            except Exception as exc:  # noqa: BLE001 - startup recovery is best-effort
                self.log.warning("startup_recover_failed", error=str(exc))
        return loop

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
