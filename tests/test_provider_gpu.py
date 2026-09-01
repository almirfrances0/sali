"""GPU safety guard: every model call takes a machine-wide single-generation lease (in-process
semaphore → resource gate → cross-process flock), so Sali can never drive two concurrent generations
onto the card and overload the machine into a shutdown — the model is only ever generating once."""

from __future__ import annotations

import asyncio

from sali.provider.ollama import (
    _INFERENCE,
    _gpu_gate,
    _gpu_lease,
    _gpu_snapshot,
    _inference_lock_fd,
)


async def test_gpu_snapshot_is_best_effort() -> None:
    # (temp, vram_frac) on a real GPU, None where nvidia-smi is unavailable (CI) — never raises.
    snap = await _gpu_snapshot()
    assert snap is None or (isinstance(snap[0], int) and 0.0 <= snap[1] <= 1.0)


async def test_gpu_gate_is_bounded() -> None:
    # The gate always completes (proceeds when the card is idle / no GPU) within its bounded backoff —
    # it must never block a turn forever.
    await asyncio.wait_for(_gpu_gate(), timeout=70)


async def test_only_one_inference_slot() -> None:
    # The module-level semaphore admits exactly one inference at a time (the in-process half).
    assert _INFERENCE._value == 1  # nothing is holding it between calls
    async with _INFERENCE:
        assert _INFERENCE.locked()  # a second concurrent inference would have to wait
    assert not _INFERENCE.locked()


async def test_lease_serializes_and_releases() -> None:
    # The lease admits one holder and releases cleanly, so back-to-back generations don't deadlock.
    async with _gpu_lease():
        assert _INFERENCE.locked()  # the lease holds the in-process slot for its whole body
    assert not _INFERENCE.locked()
    # a second, sequential lease still acquires (the cross-process flock was released)
    await asyncio.wait_for(_run_lease(), timeout=70)


async def _run_lease() -> None:
    async with _gpu_lease():
        pass


async def test_cross_process_lock_fd_is_available_or_degrades() -> None:
    # The flock fd opens (a real int) or degrades to None (semaphore-only) — it must never raise, so
    # inference is never broken by the lock file being unavailable.
    fd = _inference_lock_fd()
    assert fd is None or isinstance(fd, int)
