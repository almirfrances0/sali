"""MODEL RESIDENCY AUTHORITY — proof, not faith, that sali:latest is loaded exactly once.

The single-mind locks (:mod:`sali.core.mind`) stop Sali from *starting* a second reasoning process.
This module answers the question those locks cannot: **is the model actually loaded once right
now?** Ollama is a separate server with its own lifetime, its own memory of what it has loaded, and
its own rules — a correct Sali can still be talking to an ollama that is holding two copies.

Why that matters more here than on most machines
------------------------------------------------
``sali:latest`` is a 35B MoE with ~3B active, ~16 GB resident, on a 12 GB card. It already runs
SPLIT — roughly 60% of layers on the GPU, 40% streamed from system RAM. One copy occupies ~11.1 GB
of 12.3 GB VRAM (90.7%) plus several GB of RAM. A second copy does not "run slower": it does not
fit anywhere, and the machine thrashes RAM and swap while both halves fight for the same cores. On
this box that is not a performance problem, it is a power event.

How ollama can end up with two copies
-------------------------------------
A runner is keyed by ``(model, runner-affecting options)``. Two calls that differ in ``num_ctx``,
``num_batch``, ``num_thread`` or ``num_gpu`` get two runners of the same model — and
``OLLAMA_MAX_LOADED_MODELS`` (2 here, to allow the CPU-only embedder) permits exactly that.
:meth:`OllamaProvider._runner_options` makes those values a single stamped constant so no call site
can fork a runner. This module is the *check on that promise*, because a promise verified by
observation is worth more than a promise held by convention — and the next contributor will not
read the docstring.

What it does
------------
* :func:`survey` — what ollama actually holds this instant, from ``/api/ps``.
* :func:`assert_single_residency` — refuse to start expensive work when the chat model is loaded
  more than once. Called before cognition, cheap, and cached briefly.
* :func:`heal` — last resort: unload every copy so the next call loads exactly one.
* :func:`server_envelope` — the ollama server's own limits, read from systemd rather than assumed,
  so a drop-in lost to a package upgrade shows up as an observation instead of a surprise.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
from dataclasses import dataclass, field
from typing import Any

from sali.core.errors import ProviderError
from sali.obs.log import get_logger

log = get_logger("sali.provider.residency")

__all__ = [
    "DuplicateResidency",
    "ResidencyReport",
    "Runner",
    "assert_single_residency",
    "heal",
    "server_envelope",
    "survey",
]

# How long a clean survey stays trusted. Residency changes only when a model loads or is evicted —
# events on the scale of tens of seconds — so this keeps the guard off the hot path without letting
# a duplicate run for long.
_CACHE_TTL_S = 15.0
_cache: tuple[float, ResidencyReport] | None = None


class DuplicateResidency(ProviderError):
    """The chat model is loaded more than once. On a 12 GB card this is a host-safety emergency."""

    def __init__(self, report: ResidencyReport) -> None:
        self.report = report
        super().__init__(
            f"{report.model} is loaded {report.chat_runners}× in ollama "
            f"({report.vram_gb:.1f} GB of VRAM across {len(report.runners)} runner(s)). "
            "One copy of this 35B MoE already fills ~91% of a 12 GB card; a second does not fit "
            "anywhere and thrashes RAM and swap until the machine browns out. Refusing to add work. "
            "Runners differ only by their option set — see OllamaProvider._runner_options.")


@dataclass(slots=True)
class Runner:
    """One model instance loaded in ollama right now."""

    model: str
    size_bytes: int = 0
    vram_bytes: int = 0
    context: int = 0

    @property
    def offloaded(self) -> bool:
        """True when part of this runner lives in system RAM rather than VRAM (a split model)."""
        return self.vram_bytes < self.size_bytes


@dataclass(slots=True)
class ResidencyReport:
    model: str
    runners: list[Runner] = field(default_factory=list)
    chat_runners: int = 0
    vram_bytes: int = 0
    ok: bool = True
    reason: str = ""

    @property
    def vram_gb(self) -> float:
        return self.vram_bytes / 1e9

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model, "chat_runners": self.chat_runners,
            "total_runners": len(self.runners), "vram_gb": round(self.vram_gb, 2),
            "ok": self.ok, "reason": self.reason,
            "loaded": [{"model": r.model, "vram_gb": round(r.vram_bytes / 1e9, 2),
                        "context": r.context, "split": r.offloaded} for r in self.runners],
        }


def _same_model(loaded: str, chat_model: str) -> bool:
    """Ollama reports 'sali:latest'; a caller may configure 'sali'. Compare on the base name so an
    implicit :latest never reads as a different model — and a duplicate never hides behind a tag."""
    return loaded.split(":", 1)[0] == chat_model.split(":", 1)[0]


async def survey(client: Any, chat_model: str) -> ResidencyReport:
    """What ollama is holding this instant. Never raises — an unreadable server reports ``ok`` with
    a reason, because a diagnostic that can break inference is worse than no diagnostic."""
    report = ResidencyReport(model=chat_model)
    try:
        resp = await asyncio.wait_for(client.ps(), timeout=5.0)
    except Exception as exc:  # noqa: BLE001 - never let the guard break the call it guards
        report.reason = f"could not read ollama residency: {str(exc)[:120]}"
        return report
    for m in getattr(resp, "models", None) or []:
        runner = Runner(
            model=str(getattr(m, "model", "") or getattr(m, "name", "")),
            size_bytes=int(getattr(m, "size", 0) or 0),
            vram_bytes=int(getattr(m, "size_vram", 0) or 0),
            context=int(getattr(m, "context_length", 0) or 0),
        )
        report.runners.append(runner)
        report.vram_bytes += runner.vram_bytes
        if _same_model(runner.model, chat_model):
            report.chat_runners += 1
    report.ok = report.chat_runners <= 1
    if not report.ok:
        report.reason = f"{chat_model} loaded {report.chat_runners}×"
    return report


async def assert_single_residency(client: Any, chat_model: str, *, force: bool = False) -> None:
    """Refuse to start cognition while the chat model is loaded more than once.

    Runs before the expensive part of a turn, so a duplicate is caught before prefill commits the
    machine to a load it cannot survive. Cached for a few seconds; ``force`` bypasses the cache for
    startup checks and diagnostics.
    """
    global _cache
    now = time.monotonic()
    if not force and _cache is not None and now - _cache[0] < _CACHE_TTL_S and _cache[1].ok:
        return
    report = await survey(client, chat_model)
    _cache = (now, report)
    if not report.ok:
        log.error("duplicate_model_residency", **report.to_dict())
        raise DuplicateResidency(report)


def invalidate() -> None:
    """Drop the cached survey — call after anything that may change what ollama holds."""
    global _cache
    _cache = None


async def heal(client: Any, chat_model: str) -> ResidencyReport:
    """Last resort: evict every copy of the chat model so the next call loads exactly one.

    ``keep_alive=0`` asks ollama to release the runner immediately. Blunt on purpose — with two
    copies resident there is no safe way to choose which to keep, and a cold reload costs ~20s
    against a machine that would otherwise be at risk of powering off.
    """
    with contextlib.suppress(Exception):
        await client.generate(model=chat_model, prompt="", keep_alive=0)
    invalidate()
    await asyncio.sleep(1.0)
    report = await survey(client, chat_model)
    log.info("residency_healed", **report.to_dict())
    return report


async def server_envelope() -> dict[str, Any]:
    """Ollama's OWN limits, observed from systemd rather than assumed.

    ``OLLAMA_NUM_PARALLEL`` > 1 lets a single runner decode several sequences at once — concurrent
    generation that Sali's locks never see, because it happens inside the server.
    ``OLLAMA_MAX_LOADED_MODELS`` > 2 permits a third runner, i.e. room for a duplicate. Both are
    drop-in settings a package upgrade can silently drop, so they are read, not trusted.
    """
    env: dict[str, str] = {}
    out = ""
    if shutil.which("systemctl"):
        with contextlib.suppress(Exception):
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "show", "ollama", "-p", "Environment", "--no-pager",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            raw, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            out = raw.decode()
    for token in out.removeprefix("Environment=").split():
        key, _, value = token.partition("=")
        if key.startswith("OLLAMA_"):
            env[key] = value
    parallel = env.get("OLLAMA_NUM_PARALLEL")
    loaded = env.get("OLLAMA_MAX_LOADED_MODELS")
    problems: list[str] = []
    if parallel != "1":
        problems.append(f"OLLAMA_NUM_PARALLEL={parallel or 'unset (auto)'} — should be 1 so one "
                        "runner never decodes two sequences at once")
    if loaded not in ("1", "2"):
        problems.append(f"OLLAMA_MAX_LOADED_MODELS={loaded or 'unset (default 3×GPUs)'} — should be "
                        "2 (the chat model + the CPU-only embedder), leaving no room for a duplicate")
    return {"env": env, "ok": not problems, "problems": problems}
