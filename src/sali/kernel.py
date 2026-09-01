"""The single composition root.

``Kernel`` wires the concretes (settings, logger, provider, and a lazily-created DB pool)
so the rest of the system depends on interfaces, not construction.

The kernel owns ONE AgentRuntime per process, which in turn owns ONE AgentLoop and ONE
execution coordinator. All foreground execution (terminal, API, iOS) goes through the
runtime's submit_foreground(). Background execution (scheduler, daemon) goes through
submit_background().

This eliminates the pattern where each caller created its own AgentLoop via
kernel.agent_loop(), which caused multiple concurrent agent loops.
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
        self._runtime: Any = None  # AgentRuntime — lazy-created
        self._recovered = False

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

    async def runtime(
        self, *, confirmer: Any = None, ws_broadcaster: Any = None,
    ) -> Any:
        """Get or create the single AgentRuntime for this process.

        The runtime owns ONE AgentLoop and ONE execution coordinator.
        All foreground execution goes through runtime.submit_foreground().
        """
        if self._runtime is not None:
            return self._runtime

        from sali.context.engine import ContextEngine
        from sali.learning.service import LearningService
        from sali.retrieval.service import RetrievalService
        from sali.runtime.loop import AgentLoop
        from sali.runtime.runtime import AgentRuntime
        from sali.runtime.session import persistent_session_id
        from sali.security.confirm import AutoAllowConfirmer
        from sali.security.policy import PolicyEngine
        from sali.tools.registry import default_registry

        pool = await self.pool()
        effective_confirmer = confirmer or AutoAllowConfirmer()
        loop = AgentLoop(
            pool=pool,
            provider=self.provider,
            retrieval=RetrievalService(pool, self.provider),
            context=ContextEngine(self.provider, ctx_tokens=self.settings.model.ctx_default),
            registry=default_registry(),
            policy=PolicyEngine(),
            confirmer=effective_confirmer,
            settings=self.settings,
            learning=LearningService(pool, self.provider),
        )
        session_id = persistent_session_id()
        runtime = AgentRuntime(loop, session_id, pool, ws_broadcaster=ws_broadcaster)

        # Crash recovery — runs once per process
        if not self._recovered:
            self._recovered = True
            try:
                recovered = await runtime.recover()
                if recovered:
                    self.log.info("recovered_orphan_runs", count=len(recovered))
            except Exception as exc:  # noqa: BLE001
                self.log.warning("startup_recover_failed", error=str(exc))

        self._runtime = runtime
        # Start runtime services (watchdog, etc.)
        await runtime.start()
        self.log.info("runtime_initialized", session_id=str(session_id)[:8])
        return runtime

    async def agent_loop(self, *, confirmer: Any) -> Any:
        """DEPRECATED: Use runtime() instead.

        Returns the AgentLoop from the shared runtime for backward compatibility.
        New code should use kernel.runtime() and submit through the coordinator.
        """
        runtime = await self.runtime(confirmer=confirmer)
        return runtime.loop

    async def close(self) -> None:
        if self._runtime is not None:
            await self._runtime.aclose()
            self._runtime = None
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
