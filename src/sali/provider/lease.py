"""THE inference boundary: ONE mind, ONE generation at a time, machine-wide.

Backend-agnostic by design: it guards the one physical GPU, not any particular model server.
Both the Ollama provider (transitional) and the llama.cpp provider (target) route every model
call through here, and any future backend must do the same — which is why this lives in its own
module rather than inside a provider. (Extracted from provider/ollama.py during the llama.cpp
migration; the semantics are unchanged.)

The 35B model does not fit a 12 GB card — it runs SPLIT (most layers on the GPU, the rest on the
CPU), so a single generation already spikes GPU *and* CPU. TWO overlapping generations (the
terminal + the `sali daemon`'s background loops, or vision on top of a chat) double that draw and
were tripping the machine's power/thermal cutoff. So every model call — chat, streaming, vision,
embeddings, foreground or background, in ANY Sali process — passes through _inference_lease(),
which has four layers:
  0. the INFERENCE AUTHORITY  (sali.provider.authority — may this PROCESS think at all? Cognition
                               is the one living mind's privilege; a non-mind process is refused,
                               not queued, so a second reasoning path can never quietly open),
  1. an in-process semaphore  (serialises coroutines within one process),
  2. a cross-process file lock (flock — serialises the terminal, the daemon, and every faculty so
                               the heavy model is only ever generating ONCE machine-wide; the
                               kernel releases it automatically if a process dies mid-generation),
  3. a resource gate          (defer while the card is too hot or its VRAM is near full — read
                               AFTER the lock is won, so the reading describes the card we are
                               about to use rather than one someone else has since heated).
A single inference briefly at ~100% GPU is normal and safe — the danger is CONCURRENCY, not one
turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from sali.core.errors import ProviderError
from sali.obs.log import get_logger
from sali.provider.authority import InferenceKind, admit
from sali.provider.gpu import _gpu_snapshot

log = get_logger("sali.provider.lease")

_INFERENCE = asyncio.Semaphore(1)
_GPU_TEMP_LIMIT_C = 85       # defer a NEW generation while the GPU is at least this hot (thermal margin)
# MEASURED 2026-09-02: sali:latest alone resides at ~90.9% of this 12 GB card (size_vram 10.07 GB with
# num_ctx 24576). A 0.92 ceiling therefore left ~135 MiB of headroom and made Sali gate against its OWN
# steady state — any transient nudged it over and EVERY generation paid the full ~60s backoff below (the
# real cause of "Sali is slow", and of an apparent outage where the API never bound during startup).
# The rule's intent is "don't pile a NEW generation onto a card already near capacity"; 0.95 preserves
# that (still ~4 points / ~500 MiB of genuine slack above baseline) without gating normal operation.
# 0.985, not a "safe-looking" number, because on THIS box VRAM fullness is not a risk signal at all:
# sali:latest is a RESIDENT 35B on a 12 GB card and its footprint GROWS with the KV cache (measured
# 90.9% -> 93.6% -> 95.05% within one session). Every ceiling below ~100% therefore gets crossed
# eventually, and when it is crossed Sali stalls against ITSELF — 88 `gpu_busy_deferring_inference`
# lines in five minutes while the card sat at 35-40 degrees. The genuine protections are independent of
# this number and remain in force: the residency check (a SECOND copy of the model is the actual
# emergency), the temperature gate above (85 degrees, versus the 35-40 seen in normal work), the
# machine-wide inference flock, and the sali-power-envelope service that caps package + GPU power. What
# 0.985 still catches is the case this gate was written for: something OTHER than Sali's one model
# eating the card.
_GPU_VRAM_CEILING = 0.985
_GPU_COOL_WAIT_S = 2.0       # re-check cadence while waiting for the card to settle
_GPU_MAX_WAITS = 30          # ~60s bounded backoff, then proceed anyway — never deadlock a turn
_LEASE_TIMEOUT_S = 900.0     # wait up to this for the cross-process lock, then FAIL (never double up)
# A well-known path so EVERY Sali process on this machine shares the same lock (terminal + daemon run as
# the same user). Auto-recreated if /tmp is cleared; the lock itself is the fd's flock, not the file.
_LOCK_PATH = "/tmp/sali-gpu-inference.lock"  # noqa: S108 - intentional machine-wide well-known lock file
_lock_fd: int | None = None


def _inference_lock_fd() -> int | None:
    """Open (once per process) the fd whose flock serialises GPU inference machine-wide. None if the
    lock can't be created — inference then degrades to the in-process semaphore only, never breaks."""
    global _lock_fd
    if _lock_fd is not None:
        return _lock_fd
    try:
        _lock_fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o666)
    except OSError as exc:
        log.warning("gpu_lock_unavailable", error=str(exc))
        _lock_fd = None
    return _lock_fd


async def _gpu_gate() -> None:
    """Hold a new generation back while the card is too hot OR its VRAM is near full, so Sali never
    piles onto an already-stressed card. Bounded — always proceeds after the backoff, never deadlocks."""
    for i in range(_GPU_MAX_WAITS):
        snap = await _gpu_snapshot(max_age_s=(5.0 if i == 0 else 0.0))
        if snap is None:
            return  # no GPU / no nvidia-smi → nothing to gate on
        temp, vram = snap
        if temp < _GPU_TEMP_LIMIT_C and vram < _GPU_VRAM_CEILING:
            return
        log.warning("gpu_busy_deferring_inference", temp_c=temp, vram_frac=round(vram, 3))
        await asyncio.sleep(_GPU_COOL_WAIT_S)


@contextlib.asynccontextmanager
async def _inference_lease(
    kind: InferenceKind, *, client: Any = None, chat_model: str = "",
    residency_probe: Callable[[], Awaitable[None]] | None = None,
) -> AsyncIterator[None]:
    """THE inference boundary. Every model call in Sali passes through here, in this order:

        authority → in-process semaphore → machine-wide flock → residency proof → resource gate

    Each layer answers a different question, and none of the others can answer it:

    * **authority** — may this PROCESS think at all? (one mind)
    * **semaphore + flock** — may it think RIGHT NOW? (one generation at a time, machine-wide)
    * **residency** — is the model loaded exactly ONCE? Locks constrain Sali; they say nothing
      about what the model server is holding. Two shapes: ``residency_probe`` (the backend checks
      itself — llama.cpp serves exactly one model by construction, so the probe verifies the server
      IS serving that one model), or the Ollama ``client``/``chat_model`` survey of ``/api/ps``
      (a second copy of a 16 GB MoE on a 12 GB card is a host-safety emergency, not a slowdown).
    * **resource gate** — is the card fit to be used at all? Read last, so the measurement
      describes the card we are about to use rather than one someone else has since heated.

    Putting them in one place is deliberate: a new provider method cannot acquire the GPU lease —
    which it visibly must — without also passing every other gate.
    """
    admit(kind)
    async with _INFERENCE:
        fd = _inference_lock_fd()
        held = False
        if fd is not None:
            deadline = time.monotonic() + _LEASE_TIMEOUT_S
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held = True
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        # Never "proceed anyway": that trades a stalled turn for two concurrent
                        # generations — the exact condition that powers this machine off. Fail the
                        # call honestly; the caller retries or surfaces it.
                        raise ProviderError(
                            f"inference lease timed out after {_LEASE_TIMEOUT_S:.0f}s — another "
                            "generation still holds the machine-wide GPU lease. Refusing to run a "
                            "second generation concurrently (host safety). Check `sali status`."
                        ) from None
                    await asyncio.sleep(0.15)  # another Sali process is generating — wait our turn
        # Proof, not faith, that the model is loaded once — checked while we hold the lock, so no
        # other generation can change residency between the check and the call it protects.
        if residency_probe is not None and kind is InferenceKind.COGNITION:
            await residency_probe()
        elif client is not None and chat_model and kind is InferenceKind.COGNITION:
            from sali.provider.residency import assert_single_residency
            await assert_single_residency(client, chat_model)
        # Read the card AFTER winning the lock, not before. Gating first would have measured an idle
        # card, then waited minutes behind someone else's generation and started onto the hot card
        # that generation left behind — a stale reading is worse than none. Holding the lock while
        # the card settles is the point: nobody else should start either.
        if kind is not InferenceKind.EMBEDDING:  # embeds are CPU-only (0 VRAM) — nothing to gate on
            await _gpu_gate()
        try:
            yield
        finally:
            if held and fd is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
