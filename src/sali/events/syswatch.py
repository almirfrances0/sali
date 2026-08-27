"""System-state event source (spec §13/§78) — cheap, deterministic, no model.

The filesystem/window engine sees what Almir touches; this sees what the MACHINE does: a new listening
socket, a systemd unit that failed, a disk filling up. It polls a couple of plain commands on an
interval, DIFFS against the last snapshot, and turns each meaningful change into an Observation carrying
the importance + kind that the Attention Engine tiers (a new service → investigate; a failed service or
a full disk → notify Almir). The first poll only records a baseline — later polls emit deltas, so a
re-poll never floods (§41). Observation-only: it never touches the machine (§46/§78).

Self-contained (the events layer can't import the twin, which sits above it): its own defensive
subprocess runner that degrades to empty on any failure.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sali.events.base import EventKind, Observation, ObservationSink
from sali.obs.log import get_logger

log = get_logger("sali.events.syswatch")

_TIMEOUT = 8.0
_DISK_PRESSURE_PCT = 90  # a mount at/above this is worth flagging


async def _run(*argv: str) -> str:
    if shutil.which(argv[0]) is None:
        return ""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT)
        return out.decode("utf-8", "replace")
    except (OSError, TimeoutError):
        return ""


@dataclass(slots=True)
class SystemState:
    ports: set[str] = field(default_factory=set)           # "tcp:0.0.0.0:8080"
    failed_services: set[str] = field(default_factory=set)  # unit names
    disk_pressure: set[str] = field(default_factory=set)    # mounts at/above the threshold


def _parse_ports(ss_out: str) -> set[str]:
    ports: set[str] = set()
    for line in ss_out.splitlines()[1:]:  # skip header
        cols = line.split()
        if len(cols) < 5:
            continue
        proto = cols[0].lower()
        local = cols[4]  # e.g. 0.0.0.0:8080 or [::]:443
        if ":" in local:
            ports.add(f"{proto}:{local}")
    return ports


def _parse_failed(systemctl_out: str) -> set[str]:
    failed: set[str] = set()
    for line in systemctl_out.splitlines():
        parts = line.split()
        for tok in parts:
            if tok.endswith(".service") or tok.endswith(".socket") or tok.endswith(".timer"):
                failed.add(tok.lstrip("●* "))
                break
    return failed


def _parse_disk(df_out: str) -> set[str]:
    pressure: set[str] = set()
    for line in df_out.splitlines()[1:]:
        cols = line.split()
        if len(cols) < 6:
            continue
        pct_s, mount = cols[4], cols[5]
        if pct_s.endswith("%") and pct_s[:-1].isdigit() and int(pct_s[:-1]) >= _DISK_PRESSURE_PCT:
            pressure.add(mount)
    return pressure


class SystemWatch:
    def __init__(self, sink: ObservationSink, *, interval: float = 45.0, baseline: Any = None) -> None:
        self._sink = sink
        self._interval = interval
        self._prev: SystemState | None = None
        self._baseline = baseline  # optional events.baseline.Baseline — persists the port baseline (§19)

    async def probe(self) -> SystemState:
        """Snapshot the machine's live system state — listening ports, failed units, disk pressure."""
        ss_out = await _run("ss", "-H", "-lntu")
        failed_out = await _run("systemctl", "--failed", "--no-legend", "--plain")
        df_out = await _run("df", "-P")
        return SystemState(ports=_parse_ports(ss_out), failed_services=_parse_failed(failed_out),
                           disk_pressure=_parse_disk(df_out))

    def diff(self, prev: SystemState, cur: SystemState, now: datetime) -> list[Observation]:
        """Deterministic: the observations a transition prev→cur warrants. Pure (caller supplies now)."""
        obs: list[Observation] = []
        for port in sorted(cur.ports - prev.ports):
            obs.append(self._obs(EventKind.PORT_OPENED, f"new listening socket {port}", 0.72, now,
                                 {"sample": port}))
        for unit in sorted(cur.failed_services - prev.failed_services):
            obs.append(self._obs(EventKind.SERVICE_FAILED, f"service {unit} failed", 0.9, now,
                                 {"sample": unit}))
        for mount in sorted(cur.disk_pressure - prev.disk_pressure):
            obs.append(self._obs(EventKind.DISK_PRESSURE, f"disk {mount} is nearly full", 0.9, now,
                                 {"sample": mount}))
        return obs

    async def tick(self, now: datetime) -> list[Observation]:
        cur = await self.probe()
        if self._baseline is not None:
            # Ports use the PERSISTENT baseline (§19: recognise a port that appeared while Sali was off);
            # failed units + disk pressure remain always-notable events via the in-memory diff.
            surfaced = [self._obs(EventKind.PORT_OPENED, f"new listening socket {p}", 0.72, now,
                                  {"sample": p})
                        for p in sorted(await self._baseline.reconcile("port", cur.ports))]
            if self._prev is not None:
                surfaced += self.diff(SystemState(ports=cur.ports, failed_services=self._prev.failed_services,
                                                  disk_pressure=self._prev.disk_pressure), cur, now)
        else:
            surfaced = [] if self._prev is None else self.diff(self._prev, cur, now)  # first poll = baseline
        self._prev = cur
        for o in surfaced:
            with contextlib.suppress(Exception):  # a bad sink must never kill the watcher
                await self._sink.observe(o)
        return surfaced

    async def run(self, stop: asyncio.Event) -> None:
        """Poll on the interval until stop. Best-effort — a probe failure just yields an empty diff."""
        while not stop.is_set():
            try:
                await self.tick(datetime.now(UTC))
            except Exception as exc:  # noqa: BLE001 - never let a bad cycle kill the daemon faculty
                log.warning("syswatch tick failed: %s", exc)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._interval)

    @staticmethod
    def _obs(kind: EventKind, summary: str, importance: float, now: datetime,
             detail: dict[str, str]) -> Observation:
        return Observation(kind=kind, summary=summary, importance=importance, count=1,
                           first_at=now, last_at=now, detail=detail)
