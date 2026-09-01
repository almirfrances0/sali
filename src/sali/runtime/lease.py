"""Global foreground execution lease — PostgreSQL-backed cross-process coordination.

Ensures only ONE foreground agent turn executes across ALL Sali processes (terminal, API, iOS).
This solves the fundamental problem: Python singletons cannot coordinate separate OS processes.

Design:
- Single-row table `execution_lease` in PostgreSQL
- Advisory lock for atomic acquisition (prevents race conditions)
- Heartbeat-based lease with expiration (crash recovery)
- Background work does NOT acquire the lease (it's foreground-only)

Lease lifecycle:
1. try_acquire() — atomic: check + insert/update with advisory lock
2. heartbeat() — periodic: refresh lease expiration
3. release() — explicit: clear the lease when execution completes
4. recover_stale() — startup: reclaim expired leases from crashed processes

Safety:
- A crashed owner's lease expires after LEASE_DURATION (5 min) without heartbeat
- Advisory lock prevents two processes from simultaneously acquiring
- Release is idempotent — safe to call multiple times
- Background work never acquires foreground lease
"""

from __future__ import annotations

import os
import socket
from typing import Any
from uuid import UUID

from sali.obs.log import get_logger

log = get_logger("sali.runtime.lease")

# Lease duration: if no heartbeat for this long, the lease is considered stale.
LEASE_DURATION_SECONDS = 300  # 5 minutes

# Heartbeat interval: how often to refresh the lease during execution.
HEARTBEAT_INTERVAL_SECONDS = 30

# Advisory lock key for atomic lease acquisition.
_ADVISORY_LOCK_KEY = 0x5A11_F0E0  # "SALI FORE" in hex-ish


def _owner_id() -> str:
    """Generate a process-unique owner identifier: PID@hostname."""
    return f"{os.getpid()}@{socket.gethostname()}"


class ExecutionLease:
    """PostgreSQL-backed global foreground execution lease.

    Use this to coordinate foreground execution across separate Sali processes.
    Each process creates its own ExecutionLease instance (they share the DB).
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool
        self._owner_id = _owner_id()

    @property
    def owner_id(self) -> str:
        return self._owner_id

    async def try_acquire(
        self, run_id: UUID, session_id: UUID, origin: str = "cli",
    ) -> bool:
        """Attempt to acquire the global foreground execution lease.

        Returns True if acquired, False if another process owns it.
        Atomic: uses PostgreSQL advisory lock to prevent race conditions.
        """
        # Advisory lock ensures atomicity — two processes can't acquire simultaneously
        async with self._pool.acquire() as conn:  # noqa: SIM117 - keep the transaction scope explicit
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock($1)", _ADVISORY_LOCK_KEY)

                # Check current lease state
                current = await conn.fetchrow(
                    "SELECT owner_id, expires_at, status FROM execution_lease "
                    "WHERE id = 'foreground'")

                if current is None:
                    # No row — create one
                    await conn.execute(
                        "INSERT INTO execution_lease "
                        "(id, owner_id, run_id, session_id, origin, status, "
                        " acquired_at, heartbeat_at, expires_at) "
                        "VALUES ('foreground', $1, $2, $3, $4, 'active', now(), now(), "
                        "  now() + make_interval(secs => $5))",
                        self._owner_id, run_id, session_id, origin,
                        LEASE_DURATION_SECONDS)
                    log.info("lease_acquired", owner=self._owner_id,
                             run_id=str(run_id)[:8], origin=origin)
                    return True

                # Lease exists — check if it's available
                if current["status"] == "active":
                    expires = current["expires_at"]
                    if expires is not None and expires > _now():
                        # Lease is active and not expired — someone else owns it
                        log.debug("lease_busy", owner=current["owner_id"],
                                  expires=str(expires))
                        return False

                # Lease is expired or releasing — acquire it (clearing any stale preempt request).
                await conn.execute(
                    "UPDATE execution_lease SET "
                    "  owner_id = $1, run_id = $2, session_id = $3, origin = $4, "
                    "  status = 'active', acquired_at = now(), heartbeat_at = now(), "
                    "  expires_at = now() + make_interval(secs => $5), "
                    "  preempt_requested = false, preempt_reason = NULL, preempt_by = NULL "
                    "WHERE id = 'foreground'",
                    self._owner_id, run_id, session_id, origin,
                    LEASE_DURATION_SECONDS)
                log.info("lease_acquired", owner=self._owner_id,
                         run_id=str(run_id)[:8], origin=origin,
                         recovered_from=current["owner_id"])
                return True

    async def heartbeat(self) -> bool:
        """Refresh the lease heartbeat. Returns True if we still own it.

        Call this periodically during execution to prevent the lease from expiring.
        If another process stole the lease (shouldn't happen with proper locking),
        this returns False and the caller should stop.
        """
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE execution_lease SET "
                "  heartbeat_at = now(), "
                "  expires_at = now() + make_interval(secs => $2) "
                "WHERE id = 'foreground' AND owner_id = $1 AND status = 'active'",
                self._owner_id, LEASE_DURATION_SECONDS)
            # Check if the update actually affected a row
            affected = _affected(result)
            if affected == 0:
                log.warning("lease_lost_during_heartbeat", owner=self._owner_id)
                return False
            return True

    async def release(self) -> None:
        """Release the foreground execution lease.

        Called when execution completes (success, failure, or cancellation).
        Idempotent — safe to call multiple times.
        """
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE execution_lease SET status = 'releasing', "
                "  heartbeat_at = now() "
                "WHERE id = 'foreground' AND owner_id = $1",
                self._owner_id)
            affected = _affected(result)
            if affected > 0:
                log.info("lease_released", owner=self._owner_id)

    async def get_current(self) -> dict[str, Any] | None:
        """Get the current lease holder info. Returns None if no active lease."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT owner_id, run_id, session_id, origin, "
                "  acquired_at, heartbeat_at, expires_at, status "
                "FROM execution_lease WHERE id = 'foreground'")
            if row is None or row["status"] != "active":
                return None
            # Check if expired
            if row["expires_at"] and row["expires_at"] <= _now():
                return None
            return dict(row)

    async def is_available(self) -> bool:
        """Check if the foreground lease is available (no active owner)."""
        current = await self.get_current()
        return current is None

    async def request_preemption(self, reason: str) -> bool:
        """Ask the current foreground holder (in ANOTHER process) to yield (Prompt 2 live interrupt).
        Sets a durable flag the holder's fast poll checks; it then cancels its run at a safe boundary and
        releases the lease. No-op (False) if we already own it (same-process interrupts use the coordinator
        directly) or nobody active holds it."""
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE execution_lease SET preempt_requested = true, preempt_reason = $2, preempt_by = $3 "
                "WHERE id = 'foreground' AND status = 'active' AND owner_id <> $1 AND expires_at > now()",
                self._owner_id, reason[:200], self._owner_id)
        return _affected(result) > 0

    async def preempt_requested(self) -> bool:
        """True if another process asked US to yield the foreground lease (our fast poll checks this)."""
        async with self._pool.acquire() as conn:
            val = await conn.fetchval(
                "SELECT preempt_requested FROM execution_lease "
                "WHERE id = 'foreground' AND owner_id = $1 AND status = 'active'", self._owner_id)
        return bool(val)

    async def recover_stale(self) -> bool:
        """Recover a stale lease from a crashed process.

        Returns True if a stale lease was found and cleared.
        This should be called at startup to clean up after crashes.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock($1)", _ADVISORY_LOCK_KEY)

            current = await conn.fetchrow(
                "SELECT owner_id, expires_at, status FROM execution_lease "
                "WHERE id = 'foreground'")

            if current is None:
                return False

            if current["status"] != "active":
                return False

            expires = current["expires_at"]
            if expires is None or expires > _now():
                return False  # Not stale

            # Stale — clear it
            await conn.execute(
                "UPDATE execution_lease SET status = 'releasing', "
                "  heartbeat_at = now() WHERE id = 'foreground'")
            log.info("lease_recovered_stale", old_owner=current["owner_id"])
            return True


def _now() -> Any:
    """Get current time as a value comparable to PostgreSQL timestamptz."""
    import datetime
    return datetime.datetime.now(datetime.UTC)


def _affected(result: str) -> int:
    """Parse asyncpg's 'UPDATE N' result string."""
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError, AttributeError):
        return 0
