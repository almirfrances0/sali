"""GPU health monitoring — temperature + VRAM pressure.

Extracted from the Ollama provider so the runtime layer can check GPU state
without importing a concrete backend (import-linter rule 10).
"""

from __future__ import annotations

import asyncio
import shutil
import time

_GPU_SNAP_AT = 0.0
_GPU_SNAP_VAL: tuple[int, float] | None = None


async def gpu_pressure(*, max_age_s: float = 60.0) -> tuple[int, float] | None:
    """(temperature °C, VRAM used fraction) for callers OUTSIDE the lease, from the same cached reading.

    Public because Sali needs to be able to NOTICE the state of the card he depends on, not only be
    stopped by it. Every generation refreshes this cache, so during active use it is warm and free —
    which is the point: a check that costs a subprocess before every reply would be paid on thousands
    of healthy turns to catch the rare bad one."""
    return await _gpu_snapshot(max_age_s=max_age_s)


async def _gpu_snapshot(*, max_age_s: float = 0.0) -> tuple[int, float] | None:
    """(temperature °C, VRAM used fraction) via nvidia-smi, or None if unavailable. Read-only; a direct
    subprocess at the provider layer so nothing above needs a GPU dependency. A small ``max_age_s``
    lets the several pre-token lease acquisitions of one turn share a reading instead of paying a
    subprocess exec each (audit) — resource decisions tolerate a few seconds of staleness."""
    global _GPU_SNAP_AT, _GPU_SNAP_VAL
    if max_age_s > 0 and (time.monotonic() - _GPU_SNAP_AT) < max_age_s:
        return _GPU_SNAP_VAL
    snap: tuple[int, float] | None = None
    if shutil.which("nvidia-smi") is not None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "nvidia-smi", "--query-gpu=temperature.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
            temp_s, used_s, total_s = out.decode().strip().splitlines()[0].split(",")
            total = float(total_s) or 1.0
            snap = (int(temp_s), float(used_s) / total)
        except (OSError, ValueError, IndexError, TimeoutError):
            snap = None
    _GPU_SNAP_AT = time.monotonic()
    _GPU_SNAP_VAL = snap
    return snap
