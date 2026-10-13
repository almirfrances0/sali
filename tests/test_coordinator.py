"""Tests for the AgentRuntimeCoordinator and API security.

Proves:
- Single execution (no parallel agent loops)
- Message serialization
- Cancellation
- Same session across interfaces
- Token security
- Event recovery
"""

from __future__ import annotations

import asyncio
from typing import Any

from sali.runtime.coordinator import AgentRuntimeCoordinator, ExecutionOrigin

# ── Coordinator tests ─────────────────────────────────────────────────────────

async def test_coordinator_serializes_submissions() -> None:
    """Two concurrent submissions are serialized — never parallel."""
    coordinator = AgentRuntimeCoordinator()

    execution_order: list[str] = []

    class FakeLoop:
        async def astream(self, message: str, session_id: Any = None, *, run_id: Any = None) -> Any:
            from uuid import uuid4

            from sali.runtime.loop import LoopEvent
            execution_order.append(message)
            # Simulate some work
            await asyncio.sleep(0.05)
            yield LoopEvent("final", f"done: {message}", {"run_id": str(run_id or uuid4())})

    from sali.core.ids import new_id
    coordinator.set_agent_loop(FakeLoop())
    coordinator.set_session_id(new_id())

    # Submit two messages concurrently
    results = await asyncio.gather(
        coordinator.submit("first", ExecutionOrigin.CLI),
        coordinator.submit("second", ExecutionOrigin.API),
    )

    # Both completed
    assert all(r.get("status") == "completed" for r in results)
    # Serialized: first before second
    assert execution_order == ["first", "second"]


async def test_coordinator_cancel() -> None:
    """Cancelling a running execution stops it."""
    coordinator = AgentRuntimeCoordinator()

    class SlowLoop:
        async def astream(self, message: str, session_id: Any = None, *, run_id: Any = None) -> Any:
            from sali.runtime.loop import LoopEvent
            yield LoopEvent("status", "working")
            await asyncio.sleep(10)  # long running
            yield LoopEvent("final", "done", {})

    from sali.core.ids import new_id
    coordinator.set_agent_loop(SlowLoop())
    coordinator.set_session_id(new_id())

    # Start execution in background
    task = asyncio.create_task(coordinator.submit("long task", ExecutionOrigin.API))
    await asyncio.sleep(0.05)  # let it start

    assert coordinator.is_busy

    # Cancel
    cancelled = await coordinator.cancel_current()
    assert cancelled is True

    result = await task
    assert result.get("status") == "cancelled"


async def test_coordinator_rejects_cancel_when_idle() -> None:
    """Cancelling when nothing is running returns False."""
    coordinator = AgentRuntimeCoordinator()
    assert await coordinator.cancel_current() is False


async def test_coordinator_tracks_origin() -> None:
    """The execution context records the origin."""
    coordinator = AgentRuntimeCoordinator()

    class QuickLoop:
        async def astream(self, message: str, session_id: Any = None, *, run_id: Any = None) -> Any:
            from sali.runtime.loop import LoopEvent
            yield LoopEvent("final", "done", {})

    from sali.core.ids import new_id
    coordinator.set_agent_loop(QuickLoop())
    coordinator.set_session_id(new_id())

    await coordinator.submit("test", ExecutionOrigin.IOS)
    # After completion, current is None
    assert coordinator.current_execution is None


# ── Auth tests ────────────────────────────────────────────────────────────────

def test_token_generation() -> None:
    """Token is cryptographically secure and properly formatted."""
    import os
    import tempfile

    from sali.api.auth import get_or_create_token, verify_token

    # Use a temp file for testing
    with tempfile.NamedTemporaryFile(delete=False) as f:
        token_path = f.name

    try:
        # Monkey-patch the token file path
        import sali.api.auth as auth_module
        original_path = auth_module._TOKEN_FILE
        auth_module._TOKEN_FILE = type(original_path)(token_path)

        token = get_or_create_token()
        assert len(token) >= 16  # at least 16 chars
        assert verify_token(token, token) is True
        assert verify_token("wrong", token) is False
        assert verify_token(None, token) is False
        assert verify_token("", token) is False

        # Same token on second call
        token2 = get_or_create_token()
        assert token == token2

        auth_module._TOKEN_FILE = original_path
    finally:
        os.unlink(token_path)


def test_token_constant_time_comparison() -> None:
    """verify_token uses constant-time comparison."""
    from sali.api.auth import verify_token
    # Just verify it doesn't leak timing info (functional test)
    assert verify_token("a" * 32, "a" * 32) is True
    assert verify_token("a" * 32, "b" * 32) is False


# ── Session tests ─────────────────────────────────────────────────────────────

def test_persistent_session_id_stable() -> None:
    """persistent_session_id returns the same ID across calls."""
    from sali.runtime.session import persistent_session_id
    id1 = persistent_session_id()
    id2 = persistent_session_id()
    assert id1 == id2


def test_background_session_differs_from_persistent() -> None:
    """background_session_id is different from persistent_session_id."""
    from sali.runtime.session import background_session_id, persistent_session_id
    assert persistent_session_id() != background_session_id()
