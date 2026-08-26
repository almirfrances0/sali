"""Built-in R0 (read-only) observation tools — Sali's senses for the host.

Deterministic Python/Linux, no LLM: /proc reads and one guarded subprocess (nvidia-smi).
These let the loop answer "what is true *right now*" instead of trusting stale memory
(engineering rule 1).
"""

from __future__ import annotations

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


class MemoryInfo(Tool):
    name = "memory_info"
    description = "Current RAM and swap usage (total/available/used), in MiB."
    parameters = _NO_ARGS
    risk_level = RiskLevel.R0

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        fields: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            fields[key] = int(rest.strip().split()[0])  # kB
        total = fields["MemTotal"] // 1024
        avail = fields.get("MemAvailable", fields["MemFree"]) // 1024
        out = {
            "total_mib": total,
            "available_mib": avail,
            "used_mib": total - avail,
            "swap_total_mib": fields.get("SwapTotal", 0) // 1024,
            "swap_used_mib": (fields.get("SwapTotal", 0) - fields.get("SwapFree", 0)) // 1024,
        }
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
        path = str(args.get("path") or "/")
        usage = shutil.disk_usage(path)
        gib = 1024**3
        out = {
            "path": path,
            "total_gib": round(usage.total / gib, 1),
            "used_gib": round(usage.used / gib, 1),
            "free_gib": round(usage.free / gib, 1),
            "percent_used": round(usage.used / usage.total * 100, 1),
        }
        return ToolResult(ok=True, output=out, display=f"{out['free_gib']} GiB free on {path}")


class GpuInfo(Tool):
    name = "gpu_info"
    description = "NVIDIA GPU name, VRAM (used/total MiB), utilization, and temperature."
    parameters = _NO_ARGS
    risk_level = RiskLevel.R0
    timeout_s = 8.0

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        argv = [
            "nvidia-smi",
            "--query-gpu=name,memory.total,memory.used,utilization.gpu,temperature.gpu",
            "--format=csv,noheader,nounits",
        ]
        try:
            rc, out, err = await run_argv(argv, timeout=self.timeout_s)
        except CommandTimeout as exc:
            return ToolResult(ok=False, display="gpu timeout", error=str(exc))
        except FileNotFoundError:
            return ToolResult(ok=False, display="no nvidia-smi", error="nvidia-smi not installed")
        if rc != 0:
            return ToolResult(ok=False, display="nvidia-smi failed", error=err.strip() or f"rc={rc}")
        fields = [c.strip() for c in out.splitlines()[0].split(",")]
        name = fields[0] if fields else "unknown"
        total, used, util, temp = (_as_int(fields[i]) if i < len(fields) else None for i in (1, 2, 3, 4))
        result = {
            "name": name,
            "vram_total_mib": total,
            "vram_used_mib": used,
            "gpu_util_percent": util,
            "temperature_c": temp,
        }
        display = f"{name}: {used}/{total} MiB VRAM" + (f", {temp}C" if temp is not None else "")
        return ToolResult(ok=True, output=result, display=display)


def _as_int(value: str) -> int | None:
    """Parse a numeric nvidia-smi field, tolerating '[N/A]' / '[Not Supported]' → None."""
    value = value.strip()
    return int(value) if value.lstrip("-").isdigit() else None


def register_builtins(registry: ToolRegistry) -> None:
    for tool_cls in (SystemInfo, MemoryInfo, DiskInfo, GpuInfo):
        registry.register(tool_cls())
