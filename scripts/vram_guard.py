#!/usr/bin/env python3
"""SYSTEM-LEVEL GPU GUARD — one model server on the card, and the card may not run over the line.

This runs as a root systemd service, INDEPENDENT of Sali. Both hard power-offs on 2026-09-01 happened
with Sali NOT running — a different app drove the model server to load the model without ever
checking whether it was already loaded. So the protection cannot live inside Sali; it has to sit at the
machine level and watch the one physical resource everything shares: the GPU.

llama.cpp RETARGET (2026-10): the reasoning model is served by ONE systemd-owned llama-server
(--parallel 1, one model loaded at startup and held permanently). Unlike Ollama there is no server
API to unload a duplicate — a duplicate llama-server IS the anomaly, so the guard sheds it as a
process. The legitimate tenant is identified by START TIME: the systemd unit starts at boot, so the
OLDEST llama-server on the card is the rightful one and is never touched; anything newer is the
intruder. (Edge this cannot see: a duplicate started before the unit — then the newest one is still
shed, which is the documented, deterministic choice.)

Two dangers, two triggers — deliberately NOT "kill at 95%", because ONE copy of this 35B already
fills most of the card. A naïve 95% ceiling would execute the legitimate server the instant its KV
cache grew. The guard distinguishes the sole rightful tenant from an intruder:

  1. DUPLICATE SERVER — more than one llama-server process holding GPU compute. THIS is "loaded
     without checking if it's already loaded". The guard stops the NEWEST one (SIGTERM→SIGKILL).
     The established server is never touched.

  2. OVER-CEILING    — VRAM at/above the hard ceiling (default 96%) WHILE more than one consumer is
     present. Near-OOM with two tenants is exactly the RAM/VRAM thrash that precedes a brown-out;
     the guard sheds the newest tenant to get back under — a non-server hog is stopped the same way.

What this guard does and does NOT do (honesty matters more than reassurance):
  * It CANNOT stop a microsecond load-transient hard-off — that is faster than any poller. That danger
    is bounded in hardware by scripts/power_envelope.sh (GPU clock ceiling + CPU/GPU power caps).
  * It CAN stop a duplicate/second load and runaway VRAM — sustained states a 1 Hz poll catches, which
    is precisely the "something loads the model on its own without checking" failure the user hit.

Graceful before forceful: SIGTERM first, SIGKILL only after a grace period. A denylist protects
init/ssh/Xorg/the guard itself (self-protection is by PID, which is authoritative).
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass

# ── Tunables (env-overridable) ──────────────────────────────────────────────────────────────────────
POLL_S = float(os.environ.get("SALI_VRAM_POLL_S", "1.0"))
CEILING_FRAC = float(os.environ.get("SALI_VRAM_CEILING", "0.96"))   # above the single-server line
ACTION_COOLDOWN_S = float(os.environ.get("SALI_VRAM_COOLDOWN_S", "20"))
GRACE_S = float(os.environ.get("SALI_VRAM_GRACE_S", "3"))
# Identifies Sali's CHAT model server by its command line (read from /proc — nvidia-smi's name
# field alone cannot distinguish servers). Default matches the chat model's GGUF filename; the
# CPU-only embed server has a different cmdline and is deliberately NOT a match. Measured reason
# this must be a cmdline match: a bare "llama-server" name match also caught the embed server,
# which llama.cpp puts on the GPU process list with a ~184 MiB CUDA context even at -ngl 0.
SERVER_MARKER = os.environ.get("SALI_VRAM_SERVER_MARKER", "Qwen3.5-35B")
# A second consumer only matters if it is big enough to brown out the machine. On a desktop the
# GPU process list permanently carries small clients (compositor, IDE, remote desktop at
# 15-200 MiB); they are not hazards and must never trigger — or be shed as — "the hog".
HOG_MIN_MIB = int(os.environ.get("SALI_VRAM_HOG_MIN_MIB", "1024"))

# Never signal these — killing them is worse than the condition being guarded against.
# Never signal these by name; self-protection is by PID (os.getpid) below, which is authoritative.
PROTECTED = ("systemd", "init", "sshd", "Xorg", "Xwayland", "cloudflared", "dockerd", "containerd")


def log(event: str, **kw: object) -> None:
    """One structured line to journald. stdout is captured by systemd."""
    parts = " ".join(f"{k}={v}" for k, v in kw.items())
    print(f"[vram-guard] {event} {parts}".rstrip(), flush=True)


def _run(*args: str, timeout: float = 4.0) -> str:
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        return out.stdout if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


@dataclass(slots=True)
class ComputeApp:
    pid: int
    used_mib: int
    name: str
    started_at: float = 0.0


def gpu_state() -> tuple[float, list[ComputeApp]]:
    """(vram_used_fraction, compute apps sorted newest-first). Empty/0.0 if nvidia-smi is unreadable —
    the guard never acts on a reading it does not have."""
    mem = _run("nvidia-smi", "--query-gpu=memory.used,memory.total",
               "--format=csv,noheader,nounits")
    frac = 0.0
    if mem.strip():
        try:
            used, total = (float(x) for x in mem.strip().splitlines()[0].split(","))
            frac = used / total if total else 0.0
        except (ValueError, IndexError):
            frac = 0.0
    apps: list[ComputeApp] = []
    raw = _run("nvidia-smi", "--query-compute-apps=pid,used_memory,process_name",
               "--format=csv,noheader,nounits")
    for line in raw.strip().splitlines():
        try:
            pid_s, mib_s, name = (p.strip() for p in line.split(",", 2))
            pid = int(pid_s)
        except (ValueError, IndexError):
            continue
        apps.append(ComputeApp(pid=pid, used_mib=int(float(mib_s or 0)), name=name,
                               started_at=_proc_start(pid)))
    apps.sort(key=lambda a: a.started_at, reverse=True)  # newest first — the likely intruder
    return frac, apps


def _proc_start(pid: int) -> float:
    """Process start time (epoch-ish, from /proc). Higher = newer. 0 if gone."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            starttime = int(f.read().split(") ", 1)[1].split()[19])
        return float(starttime)  # in clock ticks since boot — monotonic ordering is all we need
    except (OSError, IndexError, ValueError):
        return 0.0


def _proc_name(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except OSError:
        return ""


def _cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\x00", b" ").decode(errors="replace")
    except OSError:
        return ""


def _is_server(app: ComputeApp) -> bool:
    """Is this GPU compute app Sali's CHAT model server — the sole rightful tenant of the card?
    Identified by its command line (the model artifact it was started with), which is the only
    signal that cannot be fooled by another llama-server instance or a renamed binary."""
    return SERVER_MARKER in _cmdline(app.pid)


def stop_pid(pid: int) -> None:
    """SIGTERM, then SIGKILL after a grace period. Refuses protected processes."""
    name = _proc_name(pid)
    if any(p in name for p in PROTECTED) or pid <= 1 or pid == os.getpid():
        log("refuse_protected", pid=pid, name=name)
        return
    try:
        os.kill(pid, signal.SIGTERM)
        log("sigterm", pid=pid, name=name)
    except ProcessLookupError:
        return
    except PermissionError:
        log("no_permission", pid=pid, name=name)
        return
    deadline = time.monotonic() + GRACE_S
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
        log("sigkill", pid=pid, name=name)
    except ProcessLookupError:
        pass


def assess_and_act(last_action: float) -> float:
    """One tick. Returns the (possibly updated) time of the last action, for cooldown."""
    frac, apps = gpu_state()

    servers = [a for a in apps if _is_server(a)]
    duplicate = len(servers) > 1
    # A second SIGNIFICANT gpu consumer while the model is resident is the other face of "loaded
    # without checking": a hand-run second model, or a heavy job grabbing VRAM. Small desktop
    # clients (below HOG_MIN_MIB) are permanent, harmless, and ignored in both directions.
    hogs = [a for a in apps if not _is_server(a) and a.used_mib >= HOG_MIN_MIB]
    over_ceiling = frac >= CEILING_FRAC

    if not (duplicate or (over_ceiling and hogs)):
        return last_action  # the sole rightful tenant, under the line — leave it be

    now = time.monotonic()
    if now - last_action < ACTION_COOLDOWN_S:
        return last_action  # already acted; give the last action time to take effect

    log("danger", vram_frac=round(frac, 3), servers=len(servers), hogs=len(hogs),
        gpu_procs=len(apps), duplicate=duplicate, over_ceiling=over_ceiling)

    # Shed the NEWEST offender, and never the established server: a duplicate chat server is the
    # newest server-marked process; a thrasher over the ceiling is the newest significant hog.
    # The chat server itself is excluded from the hog set by identity, so it cannot be picked.
    offenders = servers if duplicate else hogs
    if offenders:
        newest = max(offenders, key=lambda a: a.started_at)
        stop_pid(newest.pid)
        log("acted", action="stopped_newest_consumer", pid=newest.pid, name=newest.name,
            was_server=_is_server(newest))
        return now
    return last_action


def main() -> None:
    log("starting", poll_s=POLL_S, ceiling=CEILING_FRAC, server_marker=SERVER_MARKER,
        hog_min_mib=HOG_MIN_MIB)
    last_action = 0.0
    last_heartbeat = 0.0
    while True:
        try:
            last_action = assess_and_act(last_action)
            now = time.monotonic()
            if now - last_heartbeat > 300:  # a quiet heartbeat every 5 min so silence != death
                frac, apps = gpu_state()
                log("ok", vram_frac=round(frac, 3), gpu_procs=len(apps))
                last_heartbeat = now
        except Exception as exc:  # noqa: BLE001 - the guard must outlive any single bad tick
            log("tick_error", error=str(exc)[:120])
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
