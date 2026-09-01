"""Resource stewardship & self-preservation (§8-§16/§20) — Sali protects the host it lives on.

Sali's continued existence depends on a healthy machine, so runtime resources are first-class operational
state, observed DETERMINISTICALLY (never guessed by the model): GPU utilisation + VRAM + temperature via
nvidia-smi, RAM via /proc/meminfo, CPU load via /proc/loadavg, disk via shutil. Readings classify into a
state ladder (safe → emergency); a ResourceAuthority turns that + a work item's PRIORITY into run / defer /
reject, so curiosity yields before the machine does and the user stays responsive; and a deterministic
preservation decision says when to stop starting expensive work and shed load. This complements the
existing GPU inference lease/gate (which guards the model boundary) — here Sali reasons about the host as
a whole. No claim of consciousness or fear (§16): just a durable rule that host integrity enables
continued operation.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class ResourceState(StrEnum):
    SAFE = "safe"
    ELEVATED = "elevated"
    HIGH = "high"
    CRITICAL = "critical"
    EMERGENCY = "emergency"


_STATE_RANK = {s: i for i, s in enumerate(
    (ResourceState.SAFE, ResourceState.ELEVATED, ResourceState.HIGH,
     ResourceState.CRITICAL, ResourceState.EMERGENCY))}


@dataclass(slots=True)
class ResourceReading:
    """A deterministic snapshot of host resources. Fractions are 0..1; None where unreadable."""
    vram_used_frac: float | None = None
    gpu_util_frac: float | None = None
    gpu_temp_c: float | None = None
    ram_used_frac: float | None = None
    cpu_load_per_core: float | None = None
    disk_used_frac: float | None = None
    ok: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ResourceBudget:
    """Thresholds derived from the real machine where possible (§9). Adapt before catastrophe, not after."""
    vram_high: float = 0.85
    vram_critical: float = 0.92
    vram_emergency: float = 0.97
    ram_high: float = 0.85
    ram_critical: float = 0.93
    ram_emergency: float = 0.97
    temp_high: float = 82.0
    temp_critical: float = 87.0
    temp_emergency: float = 91.0
    disk_high: float = 0.90
    disk_critical: float = 0.96
    load_high: float = 2.0            # per-core load average
    load_critical: float = 4.0


def classify(reading: ResourceReading, budget: ResourceBudget | None = None) -> ResourceState:
    """The host state = the WORST dimension (§9/§12). Deterministic; unreadable dimensions are ignored."""
    b = budget or ResourceBudget()
    worst = ResourceState.SAFE

    def bump(to: ResourceState) -> None:
        nonlocal worst
        if _STATE_RANK[to] > _STATE_RANK[worst]:
            worst = to

    def ladder(v: float | None, high: float, crit: float, emer: float) -> None:
        if v is None:
            return
        if v >= emer:
            bump(ResourceState.EMERGENCY)
        elif v >= crit:
            bump(ResourceState.CRITICAL)
        elif v >= high:
            bump(ResourceState.HIGH)
        elif v >= high * 0.85:
            bump(ResourceState.ELEVATED)

    ladder(reading.vram_used_frac, b.vram_high, b.vram_critical, b.vram_emergency)
    ladder(reading.ram_used_frac, b.ram_high, b.ram_critical, b.ram_emergency)
    ladder(reading.gpu_temp_c, b.temp_high, b.temp_critical, b.temp_emergency)
    ladder(reading.disk_used_frac, b.disk_high, b.disk_critical, b.disk_critical + 0.02)
    ladder(reading.cpu_load_per_core, b.load_high, b.load_critical, b.load_critical * 1.5)
    return worst


# Work-priority ladder (§15): the user is always most important; curiosity yields first.
class Priority(StrEnum):
    USER = "user"                    # the user is interacting — stay responsive
    CRITICAL_TASK = "critical_task"  # the active foreground objective
    BACKGROUND = "background"        # background learning / maintenance
    CURIOSITY = "curiosity"          # lowest — self-initiated exploration


_PRIORITY_RANK = {Priority.CURIOSITY: 0, Priority.BACKGROUND: 1, Priority.CRITICAL_TASK: 2,
                  Priority.USER: 3}


@dataclass(slots=True)
class ResourceDecision:
    verdict: str                     # 'run' | 'defer' | 'reject'
    state: ResourceState
    reasons: list[str] = field(default_factory=list)


class ResourceAuthority:
    """Turns host state + work priority into run / defer / reject (§11/§15/§20). Deterministic. Curiosity
    stops first; the user stays responsive as long as possible; an emergency sheds everything but the user."""

    def decide(self, *, priority: str, state: ResourceState) -> ResourceDecision:
        try:
            p = Priority(priority)
        except ValueError:
            p = Priority.BACKGROUND
        rank = _PRIORITY_RANK[p]
        if state is ResourceState.SAFE or state is ResourceState.ELEVATED:
            return ResourceDecision("run", state, ["host healthy"])
        if state is ResourceState.HIGH:
            if rank <= _PRIORITY_RANK[Priority.CURIOSITY]:
                return ResourceDecision("defer", state, ["curiosity yields under high pressure"])
            return ResourceDecision("run", state, ["above curiosity — allowed under high pressure"])
        if state is ResourceState.CRITICAL:
            if rank <= _PRIORITY_RANK[Priority.BACKGROUND]:
                return ResourceDecision("reject", state, ["background/curiosity shed under critical pressure"])
            if rank == _PRIORITY_RANK[Priority.CRITICAL_TASK]:
                return ResourceDecision("defer", state, ["active task deferred until pressure eases"])
            return ResourceDecision("run", state, ["user interaction stays responsive"])
        # EMERGENCY — only the user is served; everything else is rejected (§12)
        if rank == _PRIORITY_RANK[Priority.USER]:
            return ResourceDecision("run", state, ["user served even in emergency"])
        return ResourceDecision("reject", state, ["emergency preservation — non-user work halted"])

    def should_preserve(self, state: ResourceState) -> bool:
        """Whether Sali should enter preservation mode — stop starting expensive work + shed load (§12)."""
        return _STATE_RANK[state] >= _STATE_RANK[ResourceState.CRITICAL]


# The deterministic preservation sequence (§12) — an ordered plan, not chain-of-thought.
PRESERVATION_STEPS: tuple[str, ...] = (
    "stop starting new expensive work",
    "cancel low-priority (curiosity/background) activity",
    "persist active task checkpoints",
    "compact or release what can safely be freed",
    "release GPU/CPU pressure",
    "avoid retries that worsen the condition",
    "notify the user",
    "wait for the environment to recover",
)


class ResourceMonitor:
    def __init__(self, *, reader: Callable[[], ResourceReading] | None = None,
                 budget: ResourceBudget | None = None) -> None:
        self._reader = reader or read_hardware
        self._budget = budget or ResourceBudget()

    async def observe(self) -> ResourceReading:
        with contextlib.suppress(Exception):
            return self._reader()
        return ResourceReading(ok=False)

    async def state(self) -> ResourceState:
        return classify(await self.observe(), self._budget)

    async def snapshot(self) -> dict[str, Any]:
        r = await self.observe()
        st = classify(r, self._budget)
        return {"state": st.value, "reading": r.to_dict(),
                "preserve": ResourceAuthority().should_preserve(st)}


def read_hardware() -> ResourceReading:
    """Best-effort deterministic host read (Linux/Kali). Each dimension is guarded independently; a
    missing dimension is None, never a guess. Never raises."""
    r = ResourceReading()
    with contextlib.suppress(Exception):
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3)
        if out.returncode == 0 and out.stdout.strip():
            util, gused, gtotal, temp = (x.strip() for x in out.stdout.strip().splitlines()[0].split(","))
            r.gpu_util_frac = float(util) / 100.0
            r.vram_used_frac = float(gused) / float(gtotal) if float(gtotal) else None
            r.gpu_temp_c = float(temp)
    with contextlib.suppress(Exception):
        meminfo: dict[str, int] = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, rest = line.partition(":")
                meminfo[k] = int(rest.strip().split()[0])  # kB
        mem_total, mem_avail = meminfo.get("MemTotal", 0), meminfo.get("MemAvailable", 0)
        if mem_total:
            r.ram_used_frac = round((mem_total - mem_avail) / mem_total, 3)
            r.ok = True
    with contextlib.suppress(Exception):
        import os
        r.cpu_load_per_core = round(os.getloadavg()[0] / (os.cpu_count() or 1), 3)
    with contextlib.suppress(Exception):
        du = shutil.disk_usage("/")
        r.disk_used_frac = round(du.used / du.total, 3) if du.total else None
    return r
