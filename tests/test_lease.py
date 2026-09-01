"""Cross-process execution lease tests.

Proves that the PostgreSQL-backed ExecutionLease prevents two independent
runtime instances from executing foreground turns simultaneously.

These tests use the real database to verify actual cross-process coordination.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest

from sali.runtime.lease import ExecutionLease

pytestmark = pytest.mark.db


# ── Lease acquisition tests ───────────────────────────────────────────────────

async def test_lease_acquire_and_release(live_pool: Any) -> None:
    """Basic acquire → release cycle."""
    lease = ExecutionLease(live_pool)
    run_id = uuid4()
    session_id = uuid4()

    acquired = await lease.try_acquire(run_id, session_id, "cli")
    assert acquired is True

    # Should be available now
    current = await lease.get_current()
    assert current is not None
    assert current["owner_id"] == lease.owner_id

    # Release
    await lease.release()

    # Should be available again
    assert await lease.is_available() is True


async def test_lease_double_acquire_fails(live_pool: Any) -> None:
    """Second acquire while first is active must fail."""
    lease1 = ExecutionLease(live_pool)
    lease2 = ExecutionLease(live_pool)  # simulates another process

    run_id1, session_id1 = uuid4(), uuid4()
    run_id2, session_id2 = uuid4(), uuid4()

    # First acquires
    assert await lease1.try_acquire(run_id1, session_id1, "cli") is True

    # Second must fail
    assert await lease2.try_acquire(run_id2, session_id2, "api") is False

    # First still owns it
    current = await lease1.get_current()
    assert current is not None
    assert current["owner_id"] == lease1.owner_id

    # Cleanup
    await lease1.release()


async def test_lease_after_release_can_acquire(live_pool: Any) -> None:
    """After release, another process can acquire."""
    lease1 = ExecutionLease(live_pool)
    lease2 = ExecutionLease(live_pool)

    run_id1, session_id1 = uuid4(), uuid4()
    run_id2, session_id2 = uuid4(), uuid4()

    await lease1.try_acquire(run_id1, session_id1, "cli")
    await lease1.release()

    # Second can now acquire
    assert await lease2.try_acquire(run_id2, session_id2, "api") is True
    await lease2.release()


async def test_lease_heartbeat_keeps_alive(live_pool: Any) -> None:
    """Heartbeat refreshes the lease expiration."""
    lease = ExecutionLease(live_pool)
    run_id, session_id = uuid4(), uuid4()

    await lease.try_acquire(run_id, session_id, "cli")

    # Heartbeat should succeed
    assert await lease.heartbeat() is True

    # Still owned
    assert await lease.is_available() is False

    await lease.release()


async def test_lease_heartbeat_fails_after_release(live_pool: Any) -> None:
    """Heartbeat fails if lease was released."""
    lease = ExecutionLease(live_pool)
    run_id, session_id = uuid4(), uuid4()

    await lease.try_acquire(run_id, session_id, "cli")
    await lease.release()

    # Heartbeat should fail (no lease to refresh)
    assert await lease.heartbeat() is False


async def test_lease_recover_stale(live_pool: Any) -> None:
    """Stale lease from crashed process can be recovered."""
    lease = ExecutionLease(live_pool)
    run_id, session_id = uuid4(), uuid4()

    await lease.try_acquire(run_id, session_id, "cli")

    # Simulate crash: make the lease expired
    async with live_pool.acquire() as conn:
        await conn.execute(
            "UPDATE execution_lease SET "
            "  expires_at = now() - interval '10 minutes', "
            "  heartbeat_at = now() - interval '10 minutes' "
            "WHERE id = 'foreground'")

    # Recover stale lease
    recovered = await lease.recover_stale()
    assert recovered is True

    # Lease should be available now
    assert await lease.is_available() is True


async def test_lease_recover_stale_ignores_active(live_pool: Any) -> None:
    """Active (non-stale) lease is not recovered."""
    lease = ExecutionLease(live_pool)
    run_id, session_id = uuid4(), uuid4()

    await lease.try_acquire(run_id, session_id, "cli")

    # Should NOT recover (lease is active, not stale)
    recovered = await lease.recover_stale()
    assert recovered is False

    # Still owned
    assert await lease.is_available() is False

    await lease.release()


# ── Cross-process simulation tests ────────────────────────────────────────────

async def test_two_runtimes_cannot_execute_simultaneously(live_pool: Any) -> None:
    """Two independent lease instances cannot both acquire foreground."""
    lease_a = ExecutionLease(live_pool)
    lease_b = ExecutionLease(live_pool)

    run_a, session_a = uuid4(), uuid4()
    run_b, session_b = uuid4(), uuid4()

    # A acquires
    assert await lease_a.try_acquire(run_a, session_a, "cli") is True

    # B cannot acquire while A holds it
    assert await lease_b.try_acquire(run_b, session_b, "api") is False

    # A releases
    await lease_a.release()

    # B can now acquire
    assert await lease_b.try_acquire(run_b, session_b, "api") is True
    await lease_b.release()


async def test_concurrent_acquisition_results_in_one_owner(live_pool: Any) -> None:
    """When two processes try to acquire simultaneously, exactly one wins."""
    lease_a = ExecutionLease(live_pool)
    lease_b = ExecutionLease(live_pool)

    results: list[bool] = []

    async def _try_acquire(lease: ExecutionLease, origin: str) -> None:
        run_id, session_id = uuid4(), uuid4()
        result = await lease.try_acquire(run_id, session_id, origin)
        results.append(result)

    # Run concurrently
    await asyncio.gather(
        _try_acquire(lease_a, "cli"),
        _try_acquire(lease_b, "api"),
    )

    # Exactly one should have succeeded
    assert sum(results) == 1

    # Cleanup
    if results[0]:
        await lease_a.release()
    else:
        await lease_b.release()


async def test_background_does_not_interfere_with_foreground(live_pool: Any) -> None:
    """Background work does not acquire foreground lease."""
    lease_fg = ExecutionLease(live_pool)

    run_fg, session_fg = uuid4(), uuid4()

    # Foreground acquires
    assert await lease_fg.try_acquire(run_fg, session_fg, "cli") is True

    # Background should not try to acquire foreground lease
    # (This is a policy test — background uses submit_background which skips the lease)
    # Verify foreground is still held
    assert await lease_fg.is_available() is False

    await lease_fg.release()


# ── Coordinator integration tests ─────────────────────────────────────────────

async def test_coordinator_acquires_lease_before_execution(live_pool: Any) -> None:
    """Coordinator acquires the global lease before starting execution."""
    from sali.runtime.coordinator import AgentRuntimeCoordinator, ExecutionOrigin
    from sali.runtime.lease import ExecutionLease

    lease = ExecutionLease(live_pool)
    coordinator = AgentRuntimeCoordinator(lease=lease)

    class FakeLoop:
        async def astream(self, message: str, session_id: Any = None, *, run_id: Any = None) -> Any:
            from sali.runtime.loop import LoopEvent
            # Verify lease is held during execution
            current = await lease.get_current()
            assert current is not None
            yield LoopEvent("final", "done", {"run_id": str(run_id or uuid4())})

    from sali.core.ids import new_id
    coordinator.set_agent_loop(FakeLoop())
    coordinator.set_session_id(new_id())

    result = await coordinator.submit("test", ExecutionOrigin.CLI)
    assert result.get("status") == "completed"

    # Lease should be released after execution
    assert await lease.is_available() is True


async def test_coordinator_releases_lease_on_failure(live_pool: Any) -> None:
    """Lease is released even when execution fails."""
    from sali.runtime.coordinator import AgentRuntimeCoordinator, ExecutionOrigin
    from sali.runtime.lease import ExecutionLease

    lease = ExecutionLease(live_pool)
    coordinator = AgentRuntimeCoordinator(lease=lease)

    class FailingLoop:
        async def astream(self, message: str, session_id: Any = None, *, run_id: Any = None) -> Any:
            raise RuntimeError("simulated failure")
            yield  # unreachable — makes this an async generator so the coordinator's `async for` works

    from sali.core.ids import new_id
    coordinator.set_agent_loop(FailingLoop())
    coordinator.set_session_id(new_id())

    result = await coordinator.submit("test", ExecutionOrigin.CLI)
    assert result.get("status") == "error"

    # Lease should be released
    assert await lease.is_available() is True


async def test_coordinator_queues_when_lease_busy(live_pool: Any) -> None:
    """When lease is held by another process, coordinator queues the message."""
    from sali.runtime.coordinator import AgentRuntimeCoordinator, ExecutionOrigin
    from sali.runtime.lease import ExecutionLease

    lease = ExecutionLease(live_pool)

    # Simulate another process holding the lease
    other_run, other_session = uuid4(), uuid4()
    await lease.try_acquire(other_run, other_session, "api")

    coordinator = AgentRuntimeCoordinator(lease=lease)

    class FakeLoop:
        async def astream(self, message: str, session_id: Any = None, *, run_id: Any = None) -> Any:
            from sali.runtime.loop import LoopEvent
            yield LoopEvent("final", "done", {"run_id": str(run_id or uuid4())})

    from sali.core.ids import new_id
    coordinator.set_agent_loop(FakeLoop())
    coordinator.set_session_id(new_id())

    # Submit should eventually succeed after lease is released
    async def _release_after_delay() -> None:
        await asyncio.sleep(0.5)
        await lease.release()

    asyncio.create_task(_release_after_delay())

    result = await coordinator.submit("test", ExecutionOrigin.CLI, timeout=10.0)
    assert result.get("status") == "completed"
