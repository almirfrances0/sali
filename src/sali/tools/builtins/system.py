"""Built-in R0 (read-only) observation tools — Sali's senses for the host.

Deterministic Python/Linux, no LLM: /proc reads and one guarded subprocess (nvidia-smi).
These let the loop answer "what is true *right now*" instead of trusting stale memory
(engineering rule 1).
"""

from __future__ import annotations

import asyncio
import platform
import shutil
from pathlib import Path
from typing import Any

from sali.core.enums import RiskLevel
from sali.tools.base import Tool, ToolResult
from sali.tools.context import ToolContext
from sali.tools.exec import CommandTimeout, run_argv
from sali.tools.registry import ToolRegistry

_NO_ARGS: dict[str, Any] = {"type": "object", "properties": {}, "required": []}


class SystemInfo(Tool):
    name = "system_info"
    description = "Operating system, kernel, and architecture of this machine."
    parameters = _NO_ARGS
    risk_level = RiskLevel.R0

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        uname = platform.uname()
        distro = ""
        try:
            for line in Path("/etc/os-release").read_text().splitlines():
                if line.startswith("PRETTY_NAME="):
                    distro = line.split("=", 1)[1].strip().strip('"')
        except OSError:
            pass
        out = {
            "system": uname.system,
            "kernel": uname.release,
            "arch": uname.machine,
            "hostname": uname.node,
            "distro": distro,
        }
        return ToolResult(ok=True, output=out, display=f"{distro or uname.system} · {uname.release}")


# Deterministic metric bodies, factored out so the World-State (runtime/world_state.py) reads them from
# the SAME source as these tools — no duplicate /proc parsing / nvidia-smi handling (§92).
def read_memory() -> dict[str, int]:
    fields: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, rest = line.partition(":")
        fields[key] = int(rest.strip().split()[0])  # kB
    total = fields["MemTotal"] // 1024
    avail = fields.get("MemAvailable", fields["MemFree"]) // 1024
    return {
        "total_mib": total, "available_mib": avail, "used_mib": total - avail,
        "swap_total_mib": fields.get("SwapTotal", 0) // 1024,
        "swap_used_mib": (fields.get("SwapTotal", 0) - fields.get("SwapFree", 0)) // 1024,
    }


def read_disk(path: str = "/") -> dict[str, Any]:
    usage = shutil.disk_usage(path)
    gib = 1024**3
    return {
        "path": path, "total_gib": round(usage.total / gib, 1), "used_gib": round(usage.used / gib, 1),
        "free_gib": round(usage.free / gib, 1), "percent_used": round(usage.used / usage.total * 100, 1),
    }


async def read_cpu() -> float:
    """Overall CPU utilisation % from a short /proc/stat delta (deterministic, ~120ms)."""
    def sample() -> tuple[int, int]:
        parts = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
        vals = [int(x) for x in parts]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        return sum(vals), idle
    t0, i0 = sample()
    await asyncio.sleep(0.12)
    t1, i1 = sample()
    dt = t1 - t0
    return round(100.0 * (1 - (i1 - i0) / dt), 1) if dt > 0 else 0.0


def read_uptime() -> int:
    return int(float(Path("/proc/uptime").read_text().split()[0]))


def read_processes() -> int:
    return sum(1 for p in Path("/proc").iterdir() if p.name.isdigit())


def read_listen_ports() -> int:
    """Count distinct listening TCP ports (state 0A) from /proc/net/tcp{,6}."""
    ports: set[str] = set()
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            for line in Path(f).read_text().splitlines()[1:]:
                cols = line.split()
                if len(cols) > 3 and cols[3] == "0A":
                    ports.add(cols[1].split(":")[1])
        except OSError:
            continue
    return len(ports)


async def read_gpu() -> dict[str, Any] | None:
    """Live GPU stats via nvidia-smi, or None if unavailable (no GPU / not installed / timeout)."""
    argv = [
        "nvidia-smi",
        "--query-gpu=name,memory.total,memory.used,utilization.gpu,temperature.gpu",
        "--format=csv,noheader,nounits",
    ]
    try:
        rc, out, _err = await run_argv(argv, timeout=8.0)
    except (CommandTimeout, FileNotFoundError):
        return None
    if rc != 0 or not out.strip():
        return None
    fields = [c.strip() for c in out.splitlines()[0].split(",")]
    total, used, util, temp = (_as_int(fields[i]) if i < len(fields) else None for i in (1, 2, 3, 4))
    return {"name": fields[0] if fields else "unknown", "vram_total_mib": total,
            "vram_used_mib": used, "gpu_util_percent": util, "temperature_c": temp}


class MemoryInfo(Tool):
    name = "memory_info"
    description = "Current RAM and swap usage (total/available/used), in MiB."
    parameters = _NO_ARGS
    risk_level = RiskLevel.R0

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        out = read_memory()
        return ToolResult(ok=True, output=out, display=f"{out['available_mib']} MiB available")


class DiskInfo(Tool):
    name = "disk_info"
    description = "Disk usage for a mount point (default '/'), in GiB."
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Mount point to inspect."}},
        "required": [],
    }
    risk_level = RiskLevel.R0

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        out = read_disk(str(args.get("path") or "/"))
        return ToolResult(ok=True, output=out, display=f"{out['free_gib']} GiB free on {out['path']}")


class GpuInfo(Tool):
    name = "gpu_info"
    description = "NVIDIA GPU name, VRAM (used/total MiB), utilization, and temperature."
    parameters = _NO_ARGS
    risk_level = RiskLevel.R0
    timeout_s = 8.0

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        result = await read_gpu()
        if result is None:
            return ToolResult(ok=False, display="no gpu", error="nvidia-smi unavailable or no NVIDIA GPU")
        display = (f"{result['name']}: {result['vram_used_mib']}/{result['vram_total_mib']} MiB VRAM"
                   + (f", {result['temperature_c']}C" if result["temperature_c"] is not None else ""))
        return ToolResult(ok=True, output=result, display=display)


def _as_int(value: str) -> int | None:
    """Parse a numeric nvidia-smi field, tolerating '[N/A]' / '[Not Supported]' → None."""
    value = value.strip()
    return int(value) if value.lstrip("-").isdigit() else None


def register_builtins(registry: ToolRegistry) -> None:
    for tool_cls in (SystemInfo, MemoryInfo, DiskInfo, GpuInfo):
        registry.register(tool_cls())
