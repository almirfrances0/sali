"""The single composition root — and the gate that makes "one Sali" structural.

``Kernel`` wires the concretes (settings, logger, provider, a lazily-created DB pool) so the rest of
the system depends on interfaces, not construction. It is also where this machine's central
invariant is enforced:

    ONE PC · ONE SALI · ONE MIND

A kernel comes in one of two shapes, and it must say which at construction:

* **the MIND** (``Kernel.become_mind()``) — the one living Sali. It alone may build the
  ``AgentRuntime``, and through it the one ``AgentLoop``, the one cognitive coordinator, and the
  one inference path. Becoming the mind means *taking machine-wide ownership*: a kernel-enforced
  file lock (:mod:`sali.core.mind`) plus PostgreSQL's own advisory lock
  (:mod:`sali.db.mind_lock`). If another Sali already lives, that fails — safely and immediately.

* **a UTILITY kernel** (``Kernel.create()``) — settings, logging, a provider handle and a pool, for
  datastore work: migrations, backups, inspection, enrolment codes. It can read and write Sali's
  memory. It can never call :meth:`runtime`, and the inference authority refuses it cognition while
  the mind is alive.

Interfaces — the terminal, the iPhone app, the REST API, the WebSocket — are neither. They are
*windows*, and they connect to the mind's runtime instead of building one. The API receives the
mind's runtime by injection (see ``sali.api.app.create_app``); the terminal attaches over the local
API it discovers from the mind lock (see ``sali.cli.main``).

Why the guard lives here rather than in a convention: ``Kernel.runtime()`` is the only door to an
``AgentRuntime`` in production code, so a guard on it is a guard on every future caller too. A
process-local memoised singleton — what this used to be — says nothing about a second *process*,
and two processes each holding "the only" runtime are two minds.
"""

from __future__ import annotations

import contextlib
from typing import Any

from sali.config.settings import Settings, load_settings
from sali.core.mind import (
    MindLock,
    ProcessRole,
    SecondMindError,
    live_holder,
    set_current_role,
)
from sali.obs.log import configure_logging, get_logger
from sali.provider.base import ModelProvider
from sali.provider.registry import build_provider


class Kernel:
    def __init__(
        self, settings: Settings, provider: ModelProvider,
        *, role: ProcessRole = ProcessRole.UTILITY,
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.role = role
        self.log = get_logger("sali")
        self._pool: Any = None
        self._runtime: Any = None  # AgentRuntime — lazy-created, MIND kernels only
        self._recovered = False
        self._mind_lock: MindLock | None = None
        self._db_mind_lock: Any = None

    @classmethod
    def create(cls, settings: Settings | None = None) -> Kernel:
        """A UTILITY kernel: datastore + provider handle, no runtime, no cognition."""
        settings = settings or load_settings()
        configure_logging(settings.log_level)
        return cls(settings, build_provider(settings), role=ProcessRole.UTILITY)

    # ── Becoming THE Sali ─────────────────────────────────────────────────────────────────────────
    @classmethod
    async def become_mind(
        cls, settings: Settings | None = None, *,
        api_host: str | None = None, api_port: int | None = None,
    ) -> Kernel:
        """Take machine-wide ownership of the one Sali, or raise :class:`SecondMindError`.

        Both locks are taken before anything expensive exists — no pool, no runtime, no model — so a
        second start costs nothing and changes nothing. The file lock goes first because it works
        with the database down, which is exactly when a duplicate is most likely to be started.
        """
        settings = settings or load_settings()
        configure_logging(settings.log_level)

        # BEFORE ownership, before a pool, before the model: can this machine survive him thinking?
        # A 35B MoE prefill drives every core and the GPU flat out at once; on an uncapped board that
        # collapsed the rail and powered the machine off mid-turn. Refusing here costs one command;
        # the alternative costs the machine. This is the host-preservation rule at its sharpest —
        # the environment Sali lives in has a limit, and he is the thing most likely to exceed it.
        from sali.runtime.envelope import preflight
        await preflight(enforce=True)

        kernel = cls(settings, build_provider(settings), role=ProcessRole.MIND)

        lock = MindLock()
        if not lock.acquire(role=ProcessRole.MIND, api_host=api_host, api_port=api_port):
            raise SecondMindError(live_holder(), attempted="runtime")
        kernel._mind_lock = lock

        from sali.db.mind_lock import DbMindLock
        db_lock = DbMindLock(settings)
        if not await db_lock.acquire():
            lock.release()
            raise SecondMindError(
                live_holder(),
                attempted="runtime (another Sali holds this datastore's mind lock)")
        kernel._db_mind_lock = db_lock

        kernel.log.info("mind_acquired", pid_lock=str(lock.path),
                        api=f"{api_host}:{api_port}" if api_port else "none")
        return kernel

    @property
    def is_mind(self) -> bool:
        """True only while this kernel holds machine-wide ownership of the one Sali."""
        return self.role is ProcessRole.MIND and self._mind_lock is not None and self._mind_lock.held

    def publish_endpoint(self, host: str, port: int) -> None:
        """Announce where interfaces can reach this mind — called once the API has actually bound.

        This is what turns the mind lock into a discovery service: `sali agent` reads it and attaches
        instead of starting a second Sali.
        """
        if self._mind_lock is not None:
            self._mind_lock.publish(api_host=host, api_port=port)

    # ── Resources ─────────────────────────────────────────────────────────────────────────────────
    async def pool(self) -> Any:
        if self._pool is None:
            from sali.db.pool import create_pool
            self._pool = await create_pool(self.settings)
        return self._pool

    async def runtime(
        self, *, confirmer: Any = None, ws_broadcaster: Any = None,
    ) -> Any:
        """THE AgentRuntime — one per machine, not one per process.

        Owns the one AgentLoop and the one cognitive coordinator. Every foreground message (terminal,
        API, iPhone) and every background turn (scheduler, perception, initiative) is arbitrated
        through it. Only a kernel that holds machine-wide ownership may open this door.
        """
        if not self.is_mind:
            raise SecondMindError(live_holder(), attempted="AgentRuntime")
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
            retrieval=RetrievalService(pool, self.provider,
                                       owner_timezone=self.settings.temporal.owner_timezone),
            context=ContextEngine(self.provider, ctx_tokens=self.settings.model.ctx_default),
            registry=default_registry(),
            policy=PolicyEngine(),
            confirmer=effective_confirmer,
            settings=self.settings,
            learning=LearningService(pool, self.provider),
        )
        session_id = persistent_session_id()
        runtime = AgentRuntime(loop, session_id, pool, ws_broadcaster=ws_broadcaster)
        if self._mind_lock is not None:
            self._mind_lock.publish(session_id=str(session_id))

        # Crash recovery — runs once per process
        if not self._recovered:
            self._recovered = True
            # We hold machine-wide ownership, so no other Sali exists and any foreground lease still
            # marked 'active' belongs to a process that died. Clear it now rather than making Almir
            # wait out the 300s expiry the lease needs when it cannot observe process death.
            with contextlib.suppress(Exception):
                await runtime.lease.reclaim_orphaned()
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
        """DEPRECATED: use :meth:`runtime`. Kept only so old call sites reach the SAME loop."""
        runtime = await self.runtime(confirmer=confirmer)
        return runtime.loop

    async def close(self) -> None:
        """Tear down, releasing machine-wide ownership last so nothing outlives the lock."""
        if self._runtime is not None:
            await self._runtime.aclose()
            self._runtime = None
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
        if self._db_mind_lock is not None:
            with contextlib.suppress(Exception):
                await self._db_mind_lock.release()
            self._db_mind_lock = None
        if self._mind_lock is not None:
            self._mind_lock.release()
            self._mind_lock = None
            set_current_role(ProcessRole.UNKNOWN)
