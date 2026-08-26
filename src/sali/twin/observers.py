"""Deterministic desktop observers (spec §15).

Each observer inspects one facet of the machine with plain Python/Linux — /proc, /sys,
/etc/os-release, and a few well-known ``--version`` calls — and returns structured facts. No
model is ever involved: "is Ollama installed and what version" is a parse, not a question for
Qwen (§15). Every observer is defensive: a missing tool or an odd system yields empty/partial
data, never an exception that breaks the snapshot.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import socket
from pathlib import Path

from sali.obs.log import get_logger
from sali.twin.model import TwinEntity, TwinSnapshot

log = get_logger("sali.twin.observers")

_TIMEOUT = 8.0


async def _run(*argv: str, timeout: float = _TIMEOUT) -> str:
    """Run a command, return stdout (stripped) or '' on any failure — observers never raise."""
    if shutil.which(argv[0]) is None:
        return ""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return out.decode("utf-8", "replace").strip()
    except (OSError, TimeoutError):
        return ""


def _read(path: str) -> str:
    try:
        return Path(path).read_text("utf-8", "replace")
    except OSError:
        return ""


def _first_version(text: str) -> str:
    m = re.search(r"\d+\.\d+(?:\.\d+)?", text)
    return m.group(0) if m else text.splitlines()[0].strip() if text else ""


# ---- machine identity + OS -------------------------------------------------------------------
def observe_machine() -> tuple[str, str, dict[str, str]]:
    """Return (canonical_key, display_name, props) for the machine itself."""
    host = socket.gethostname() or "localhost"
    osr = _read("/etc/os-release")
    pretty = ""
    for line in osr.splitlines():
        if line.startswith("PRETTY_NAME="):
            pretty = line.split("=", 1)[1].strip().strip('"')
    props = {"hostname": host, "os": pretty, "arch": os.uname().machine}
    return f"machine:{host}", pretty or host, props


async def observe_os_kernel() -> dict[str, str]:
    uname = os.uname()
    return {"kernel": uname.release, "kernel_version": uname.version.split()[0] if uname.version else ""}


# ---- hardware --------------------------------------------------------------------------------
def observe_cpu() -> TwinEntity | None:
    info = _read("/proc/cpuinfo")
    model = ""
    for line in info.splitlines():
        if line.lower().startswith("model name"):
            model = line.split(":", 1)[1].strip()
            break
    threads = os.cpu_count() or 0
    cores = len({
        ln.split(":", 1)[1].strip() for ln in info.splitlines() if ln.lower().startswith("core id")
    }) or None
    if not model and not threads:
        return None
    return TwinEntity(
        kind="hardware", key="hw:cpu", name=model or "CPU",
        props={"threads": threads, "cores": cores}, relation="has",
    )


def observe_memory() -> TwinEntity | None:
    info = _read("/proc/meminfo")
    m = re.search(r"MemTotal:\s+(\d+)\s+kB", info)
    if not m:
        return None
    gib = round(int(m.group(1)) / (1024 * 1024), 1)
    return TwinEntity(kind="hardware", key="hw:memory", name=f"{gib} GiB RAM",
                      props={"total_gib": gib}, relation="has")


async def observe_gpu() -> TwinEntity | None:
    out = await _run("nvidia-smi", "--query-gpu=name,memory.total,driver_version",
                     "--format=csv,noheader")
    if out:
        parts = [p.strip() for p in out.splitlines()[0].split(",")]
        name = parts[0] if parts else "GPU"
        props: dict[str, object] = {"vendor": "nvidia"}
        if len(parts) >= 2:
            props["vram"] = parts[1]
        if len(parts) >= 3:
            props["driver"] = parts[2]
        return TwinEntity(kind="hardware", key="hw:gpu", name=name, props=props, relation="has")
    vga = await _run("bash", "-c", "lspci 2>/dev/null | grep -i 'vga\\|3d\\|display' | head -1")
    if vga:
        return TwinEntity(kind="hardware", key="hw:gpu", name=vga.split(":")[-1].strip() or "GPU",
                          props={}, relation="has")
    return None


def observe_storage() -> list[TwinEntity]:
    try:
        usage = shutil.disk_usage("/")
    except OSError:
        return []
    total_gb = round(usage.total / 1e9)
    return [TwinEntity(kind="hardware", key="hw:storage:root", name=f"Root disk ({total_gb} GB)",
                       props={"total_gb": total_gb, "mount": "/"}, relation="has")]


# ---- software ---------------------------------------------------------------------------------
_SOFTWARE = [
    ("ollama", ("ollama", "--version"), "Ollama"),
    ("docker", ("docker", "--version"), "Docker"),
    ("git", ("git", "--version"), "Git"),
    ("python", ("python3", "--version"), "Python"),
    ("node", ("node", "--version"), "Node.js"),
    ("psql", ("psql", "--version"), "PostgreSQL"),
    ("rustc", ("rustc", "--version"), "Rust"),
    ("go", ("go", "version"), "Go"),
]


async def observe_software() -> list[TwinEntity]:
    entities: list[TwinEntity] = []
    for key, argv, label in _SOFTWARE:
        if shutil.which(argv[0]) is None:
            continue
        version = _first_version(await _run(*argv))
        name = f"{label} {version}".strip()
        entities.append(TwinEntity(kind="software", key=f"software:{key}", name=name,
                                   props={"version": version, "path": shutil.which(argv[0]) or ""},
                                   relation="runs"))
    return entities


async def observe_ollama_models() -> list[TwinEntity]:
    out = await _run("ollama", "list")
    if not out:
        return []
    entities: list[TwinEntity] = []
    for line in out.splitlines()[1:]:  # skip the header row
        cols = line.split()
        if not cols:
            continue
        name = cols[0]
        size = " ".join(cols[2:4]) if len(cols) >= 4 else ""
        entities.append(TwinEntity(kind="model", key=f"model:{name}", name=name,
                                   props={"size": size}, relation="has_model"))
    return entities


# ---- projects (git repos under home) ----------------------------------------------------------
async def observe_projects(*, exclude: tuple[str, ...] = ()) -> list[TwinEntity]:
    home = Path.home()
    excluded = {Path(p).expanduser().resolve() for p in exclude}
    entities: list[TwinEntity] = []
    seen: set[Path] = set()
    # Shallow scan: a git repo is a dir containing .git. Look at home and its immediate
    # subtrees (Desktop, projects, code, …) a couple levels deep — cheap and covers the norm.
    roots = [home, *[d for d in home.iterdir() if d.is_dir() and not d.name.startswith(".")]] \
        if home.is_dir() else []
    for root in roots[:40]:
        for gitdir in list(root.glob(".git"))[:1] + list(root.glob("*/.git"))[:60]:
            repo = gitdir.parent.resolve()
            if repo in seen or any(repo == e or e in repo.parents for e in excluded):
                continue
            seen.add(repo)
            remote = await _run("git", "-C", str(repo), "config", "--get", "remote.origin.url")
            branch = await _run("git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD")
            entities.append(TwinEntity(
                kind="project", key=f"project:{repo.name}", name=repo.name,
                props={"path": str(repo), "remote": remote, "branch": branch}, relation="hosts",
            ))
            if len(entities) >= 50:
                return entities
    return entities


async def build_snapshot(*, exclude_projects: tuple[str, ...] = ()) -> TwinSnapshot:
    """Run every observer concurrently and assemble the whole-machine structural snapshot."""
    key, name, mprops = observe_machine()
    kernel, gpu, software, models, projects = await asyncio.gather(
        observe_os_kernel(), observe_gpu(), observe_software(),
        observe_ollama_models(), observe_projects(exclude=exclude_projects),
    )
    mprops.update(kernel)
    entities: list[TwinEntity] = []
    for maybe in (observe_cpu(), observe_memory(), gpu):
        if maybe is not None:
            entities.append(maybe)
    entities += observe_storage()
    entities += software
    entities += models
    entities += projects
    return TwinSnapshot(machine_key=key, machine_name=name, machine_props=mprops, entities=entities)
