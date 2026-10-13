"""POWER ENVELOPE — Sali refuses to think on a machine that cannot survive him thinking.

On 2026-09-01 this machine hard-powered-off during a single, legitimate turn. Not a duplicate model
load: ollama had one runner, one slot, one task. The journal simply stops mid-prompt-processing —
no thermal event, no MCE, no OOM-kill. A clean rail collapse.

The arithmetic, once measured rather than assumed:

    CPU package limit   255 W   (an i9-11900K rated 125 W, unlocked to double by the board)
    GPU power limit     140 W
                        -----
                        395 W + platform, sustained, for a 20-second prefill

``sali:latest`` is a 35B MoE with ~3B active. That figure describes GENERATION and is dangerously
misleading about PREFILL: a prompt-processing batch routes across essentially every expert, so
prefill touches all 35B of weights. With ~40% of layers offloaded it streams expert weights through
every core at full tilt while the GPU runs flat out — the highest combined draw the box can reach.
The 140 W GPU cap could not help, because the GPU was never the larger half.

So the envelope is enforced in hardware (``scripts/power_envelope.sh``: RAPL + nvidia-smi), and this
module is the runtime's *check that it was*. Sali refuses to start his life on an uncapped machine
rather than discovering the limit the way he did the first time — a refusal costs one command
(``sudo systemctl start sali-power-envelope``); the alternative costs the machine.

This is the deepest form of the host-preservation rule: the environment Sali exists in has a limit,
he is the thing most likely to exceed it, and he checks before rather than reacts after.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sali.obs.log import get_logger

log = get_logger("sali.runtime.envelope")

__all__ = ["EnvelopeReport", "PowerEnvelopeUnsafe", "preflight", "read_envelope"]

_RAPL = Path("/sys/class/powercap/intel-rapl:0")

# The GPU ceiling Sali will start under. Below the 140 W that was in force when the machine died,
# because that cap was chosen while the CPU half of the load was believed to be small.
GPU_MAX_SAFE_W = 120.0
# CPU package sustained ceiling: never above the chip's own rating, whatever the board allows.
CPU_MAX_SAFE_W = 125


class PowerEnvelopeUnsafe(RuntimeError):
    """The host is not capped to a level it can sustain. Refuse to load the model."""

    def __init__(self, report: EnvelopeReport) -> None:
        self.report = report
        super().__init__(
            "Refusing to start Sali: this machine is not inside a power envelope it can survive.\n  "
            + "\n  ".join(report.problems)
            + "\n\nApply it and start again:\n"
              "    sudo cp systemd/sali-power-envelope.service /etc/systemd/system/\n"
              "    sudo systemctl daemon-reload && sudo systemctl enable --now sali-power-envelope\n"
              "\nThis is the cap that stops a 35B MoE prefill from tripping the PSU "
              "(see scripts/power_envelope.sh).")


@dataclass(slots=True)
class EnvelopeReport:
    cpu_long_w: int | None = None
    cpu_short_w: int | None = None
    cpu_rated_w: int | None = None
    gpu_limit_w: float | None = None
    gpu_max_w: float | None = None
    ok: bool = True
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def summary(self) -> str:
        cpu = f"{self.cpu_long_w}W" if self.cpu_long_w is not None else "uncapped"
        gpu = f"{self.gpu_limit_w:.0f}W" if self.gpu_limit_w is not None else "uncapped"
        return f"CPU {cpu} + GPU {gpu}"


def _uw(name: str) -> int | None:
    with contextlib.suppress(Exception):
        return int((_RAPL / name).read_text().strip()) // 1_000_000
    return None


def read_envelope() -> EnvelopeReport:
    """Measure the caps actually in force. Never raises; an unreadable dimension is None, not a guess.

    A dimension we cannot read is reported as a problem rather than assumed safe — the whole point
    is that this machine's real limit was discovered by exceeding it.
    """
    r = EnvelopeReport()
    r.cpu_long_w = _uw("constraint_0_power_limit_uw")
    r.cpu_short_w = _uw("constraint_1_power_limit_uw")
    r.cpu_rated_w = _uw("constraint_0_max_power_uw")

    if shutil.which("nvidia-smi"):
        with contextlib.suppress(Exception):
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=power.limit,power.max_limit",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5, check=False)
            if out.returncode == 0 and out.stdout.strip():
                lim, mx = (x.strip() for x in out.stdout.strip().splitlines()[0].split(","))
                r.gpu_limit_w, r.gpu_max_w = float(lim), float(mx)

    if _RAPL.exists():
        ceiling = min(CPU_MAX_SAFE_W, r.cpu_rated_w or CPU_MAX_SAFE_W)
        if r.cpu_long_w is None:
            r.problems.append("CPU package power limit is unreadable — cannot confirm a cap")
        elif r.cpu_long_w > ceiling:
            r.problems.append(
                f"CPU package sustained limit is {r.cpu_long_w}W, above the {ceiling}W this chip is "
                f"rated for. A MoE prefill runs every core flat out; combined with the GPU that is "
                "what collapsed the rail.")
    if r.gpu_limit_w is not None and r.gpu_limit_w > GPU_MAX_SAFE_W:
        r.problems.append(
            f"GPU power limit is {r.gpu_limit_w:.0f}W, above the {GPU_MAX_SAFE_W:.0f}W ceiling "
            "chosen once the CPU half of the load was measured.")
    r.ok = not r.problems
    return r


async def preflight(
    client: Any = None, chat_model: str = "", *, enforce: bool = True,
) -> dict[str, Any]:
    """Everything that must be true before Sali loads the model, checked by observation.

    Three independent facts, none of which the others imply:

    * the **host envelope** — can this machine sustain a prefill at all?
    * the **ollama server envelope** — can the server decode two sequences, or hold a third runner?
    * **residency** — is the chat model already loaded more than once?

    With ``enforce`` the first is fatal, because it is the one that ends in a power cut. The other
    two are reported: they are hazards Sali can still refuse work over at the inference boundary.
    """
    env = read_envelope()
    result: dict[str, Any] = {"envelope": env.to_dict(), "summary": env.summary}

    if client is not None and chat_model:
        from sali.provider.residency import server_envelope, survey
        server = await server_envelope()
        report = await survey(client, chat_model)
        result["ollama_server"] = server
        result["residency"] = report.to_dict()
        for problem in server["problems"]:
            log.warning("ollama_envelope_hazard", problem=problem)
        if not report.ok:
            log.error("duplicate_model_residency_at_startup", **report.to_dict())

    if env.ok:
        log.info("power_envelope_ok", **env.to_dict())
    else:
        for problem in env.problems:
            log.error("power_envelope_unsafe", problem=problem)
        if enforce:
            raise PowerEnvelopeUnsafe(env)
    return result
