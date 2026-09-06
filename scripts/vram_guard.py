#!/usr/bin/env python3
"""SYSTEM-LEVEL GPU GUARD — nothing may load the model twice, and the card may not run over the line.

This runs as a root systemd service, INDEPENDENT of Sali. Both hard power-offs on 2026-09-01 happened
with Sali NOT running — a different app (a Docker stack) drove ollama to load the model without ever
checking whether it was already loaded. So the protection cannot live inside Sali; it has to sit at the
machine level and watch the one physical resource everything shares: the 12 GB card.

Two dangers, two triggers — deliberately NOT "kill at 95%", because ONE copy of this 35B MoE already
fills ~91% of the card. A naïve 95% ceiling would execute the legitimate model the instant its KV cache
grew. The guard distinguishes the sole rightful tenant from an intruder:

  1. DUPLICATE LOAD  — the chat model resident more than once, OR a second independent GPU compute
     process appearing while the model is already loaded. THIS is "loaded without checking if it's
     already loaded". The guard unloads the model (graceful, via ollama) or stops the newest intruder
     PID. The established single model is never touched.

  2. OVER-CEILING    — VRAM at/above the hard ceiling (default 96%, above the ~91% single-model
     baseline) WHILE more than one consumer is present. Near-OOM with two tenants is exactly the
     RAM/VRAM thrash that precedes a brown-out; the guard sheds the newest tenant to get back under.

What this guard does and does NOT do (honesty matters more than reassurance):
  * It CANNOT stop a microsecond load-transient hard-off — that is faster than any poller. That danger
    is bounded in hardware by scripts/power_envelope.sh (GPU clock ceiling + CPU/GPU power caps).
  * It CAN stop a duplicate/second load and runaway VRAM — sustained states a 1 Hz poll catches, which
    is precisely the "something loads the model on its own without checking" failure the user hit.

Graceful before forceful: for ollama it asks the server to unload (keep_alive=0), which stops the model
without killing the server. Only a non-ollama GPU hog, or an unload that does not take, escalates to
SIGTERM→SIGKILL of the offending PID. A denylist protects init/ssh/Xorg/the ollama server master/itself.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import urllib.request
from dataclasses import dataclass

# ── Tunables (env-overridable) ──────────────────────────────────────────────────────────────────────
POLL_S = float(os.environ.get("SALI_VRAM_POLL_S", "1.0"))
CEILING_FRAC = float(os.environ.get("SALI_VRAM_CEILING", "0.96"))   # above the ~0.91 single-model line
CHAT_MODEL = os.environ.get("SALI_CHAT_MODEL", "sali:latest")
OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
ACTION_COOLDOWN_S = float(os.environ.get("SALI_VRAM_COOLDOWN_S", "20"))
GRACE_S = float(os.environ.get("SALI_VRAM_GRACE_S", "3"))

# Never signal these — killing them is worse than the condition being guarded against.
# Never signal these by name; self-protection is by PID (os.getpid) below, which is authoritative.
# Note: on this host the GPU compute process is always an ollama *runner* subprocess (Sali talks to
# ollama over HTTP and never appears on the GPU itself), so the PID-kill path is reserved for a genuine
# rogue non-ollama GPU hog and essentially never touches Sali.
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


def ollama_ps() -> list[dict[str, object]]:
    """Loaded runners from ollama, or [] if unreachable."""
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/ps", timeout=3) as r:  # noqa: S310 - fixed localhost
            return list(json.loads(r.read()).get("models", []))
    except Exception:  # noqa: BLE001 - a diagnostic must never crash the guard loop
        return []


def _base(model: str) -> str:
    return model.split(":", 1)[0]


def ollama_unload(model: str) -> bool:
    """Ask ollama to release a model NOW (keep_alive=0). Graceful — leaves the server running."""
    body = json.dumps({"model": model, "keep_alive": 0, "prompt": ""}).encode()
    req = urllib.request.Request(f"{OLLAMA}/api/generate", data=body,  # noqa: S310 - fixed localhost
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
            r.read()
        return True
    except Exception as exc:  # noqa: BLE001
        log("unload_failed", model=model, error=str(exc)[:80])
        return False


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
    runners = ollama_ps()
    chat_runners = [m for m in runners if _base(str(m.get("model", ""))) == _base(CHAT_MODEL)]

    duplicate = len(chat_runners) > 1
    # A second INDEPENDENT gpu compute process while the model is already resident is the other face of
    # "loaded without checking": e.g. a fresh `ollama run` runner, or a non-ollama job grabbing VRAM.
    second_consumer = len(apps) > 1
    over_ceiling = frac >= CEILING_FRAC

    if not (duplicate or (over_ceiling and second_consumer)):
        return last_action  # the sole rightful tenant, under the line — leave it be

    now = time.monotonic()
    if now - last_action < ACTION_COOLDOWN_S:
        return last_action  # already acted; give the last action time to take effect

    log("danger", vram_frac=round(frac, 3), chat_runners=len(chat_runners),
        gpu_procs=len(apps), duplicate=duplicate, over_ceiling=over_ceiling)

    # 1. Duplicate model residency → unload the model entirely (blunt but safe: with two identical
    #    runners there is no API to keep exactly one, and a cold reload costs ~20s vs a power-off).
    if duplicate:
        if ollama_unload(CHAT_MODEL):
            log("acted", action="unloaded_duplicate_model", model=CHAT_MODEL)
            return now
    # 2. Over the ceiling with more than one consumer → shed the NEWEST gpu process. If it is an ollama
    #    runner, prefer a graceful model unload; otherwise stop the pid.
    if over_ceiling and apps:
        newest = apps[0]
        if "ollama" in newest.name or _proc_name(newest.pid).startswith("ollama"):
            if ollama_unload(CHAT_MODEL):
                log("acted", action="unloaded_over_ceiling", pid=newest.pid)
                return now
        stop_pid(newest.pid)
        log("acted", action="stopped_newest_consumer", pid=newest.pid, name=newest.name)
        return now
    return last_action


def main() -> None:
    log("starting", poll_s=POLL_S, ceiling=CEILING_FRAC, model=CHAT_MODEL, ollama=OLLAMA)
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
