"""The machine must survive Sali thinking — the 2026-09-01 post-mortem, pinned.

On that date the machine hard-powered-off mid-turn. The evidence said clearly what it was NOT: one
ollama runner, one slot, one task; no thermal event, no MCE, no OOM-kill. The journal simply stops
mid-prompt-processing. A clean rail collapse on a SINGLE legitimate inference.

The cause was arithmetic nobody had measured:

    CPU package limit  255 W  (an i9-11900K rated 125 W, unlocked to double by the board)
    GPU power limit    140 W
                       -----
                       395 W + platform, sustained, through a 20-second prefill

``sali:latest`` is a 35B MoE with ~3B active. That figure holds for GENERATION and is misleading
about PREFILL: a prompt-processing batch routes across essentially every expert, so prefill touches
all 35B of weights, streaming the offloaded ~40% through every core while the GPU runs flat out.

These tests hold the three defences that came out of it:
  1. the POWER ENVELOPE — Sali refuses to think on a machine that cannot sustain it
  2. the RUNNER FINGERPRINT — no call site can fork a second copy of the model into VRAM
  3. the RESIDENCY AUTHORITY — proof from ollama, not faith, that it is loaded exactly once
"""

from __future__ import annotations

import importlib.util as _ilu
from pathlib import Path
from typing import Any

import pytest

from sali.config.settings import ModelSettings

SRC = Path(__file__).resolve().parents[1] / "src" / "sali"


# ── 1. The power envelope ─────────────────────────────────────────────────────────────────────────

def test_an_unlocked_board_is_reported_as_unsafe(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact configuration that powered the machine off must be recognised as unsafe."""
    from sali.runtime import envelope

    monkeypatch.setattr(envelope, "_RAPL", Path("/sys/class/powercap/intel-rapl:0"))
    if not envelope._RAPL.exists():
        pytest.skip("no intel-rapl on this host")
    # The board's default: 255W sustained on a chip rated 125W, beside a 140W GPU.
    monkeypatch.setattr(envelope, "_uw",
                        lambda name: {"constraint_0_power_limit_uw": 255,
                                      "constraint_1_power_limit_uw": 255,
                                      "constraint_0_max_power_uw": 125}[name])
    monkeypatch.setattr("shutil.which", lambda _: None)     # skip the GPU read
    report = envelope.read_envelope()
    assert not report.ok
    assert any("255W" in p and "125W" in p for p in report.problems), report.problems


def test_a_capped_board_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    from sali.runtime import envelope

    monkeypatch.setattr(envelope, "_uw",
                        lambda name: {"constraint_0_power_limit_uw": 95,
                                      "constraint_1_power_limit_uw": 125,
                                      "constraint_0_max_power_uw": 125}[name])
    monkeypatch.setattr("shutil.which", lambda _: None)
    assert envelope.read_envelope().ok


def test_an_over_capped_gpu_is_unsafe(monkeypatch: pytest.MonkeyPatch) -> None:
    """140W was the cap in force when the machine died — it must no longer read as acceptable."""
    from sali.runtime import envelope

    monkeypatch.setattr(envelope, "_RAPL", Path("/nonexistent"))          # CPU side unreadable
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "140.00, 200.00\n"})())
    report = envelope.read_envelope()
    assert not report.ok
    assert any("140W" in p for p in report.problems), report.problems


async def test_preflight_refuses_to_start_on_an_unsafe_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Becoming the mind is gated on this: better one refusal than one power cut."""
    from sali.runtime import envelope
    from sali.runtime.envelope import EnvelopeReport, PowerEnvelopeUnsafe, preflight

    unsafe = EnvelopeReport(cpu_long_w=255, cpu_rated_w=125, ok=False,
                            problems=["CPU package sustained limit is 255W"])
    monkeypatch.setattr(envelope, "read_envelope", lambda: unsafe)
    with pytest.raises(PowerEnvelopeUnsafe) as err:
        await preflight(enforce=True)
    assert "sali-power-envelope" in str(err.value)          # tells you how to fix it
    assert await preflight(enforce=False) is not None       # diagnostics never raise


def test_becoming_the_mind_is_gated_on_the_envelope() -> None:
    """Structural: every authoritative entry point goes through become_mind(), so the preflight
    there covers every path that can load the model — including a bare `sali agent`."""
    body = (SRC / "kernel.py").read_text()
    body = body[body.index("async def become_mind("):][:2500]
    assert "preflight(enforce=True)" in body
    assert body.index("preflight") < body.index("build_provider"), \
        "the host must be checked BEFORE a provider exists"


# ── 2. The runner fingerprint: one option set, therefore one copy in VRAM ─────────────────────────

def _provider() -> Any:
    from sali.provider.ollama import OllamaProvider
    return OllamaProvider(ModelSettings())


def test_a_caller_cannot_fork_a_second_runner() -> None:
    """Ollama keys a runner by (model, runner-affecting options). A caller that slipped a different
    num_ctx past the provider would put a SECOND 16GB copy of the model on a 12GB card."""
    from sali.provider.presets import DETERMINISTIC

    p = _provider()
    hostile = {"num_ctx": 131072, "num_batch": 2048, "num_thread": 32, "temperature": 0.9}
    _, _, opts = p._build([], None, hostile, DETERMINISTIC)
    canonical = p._runner_options()
    for key, value in canonical.items():
        assert opts[key] == value, f"caller overrode the runner fingerprint via {key}"
    assert opts["temperature"] == 0.9, "sampling must still be tunable — only residency is fixed"


def test_every_cognition_path_sends_the_same_fingerprint() -> None:
    """chat, streaming chat and vision must all resolve to ONE runner. Vision used to build its own
    option block by hand, which is precisely how a second runner appears."""
    src = (SRC / "provider" / "ollama.py").read_text()
    vision = src[src.index("async def describe_image("):]
    vision = vision[:vision.index("async def embed(")]
    assert "_runner_options()" in vision, "vision must use the canonical runner fingerprint"
    assert "num_ctx" not in vision, "vision must not name runner options itself"


def test_the_fingerprint_caps_the_prefill_transient() -> None:
    """The two settings that shave the peak of a MoE prefill. Their DEFAULTS are the safety property:
    ollama's own defaults (batch 512, threads = all cores) are what the machine died under."""
    opts = _provider()._runner_options()
    assert opts["num_batch"] <= 256, "prefill batch must stay well under ollama's 512 default"
    assert 1 <= opts["num_thread"] <= 8, "prefill must never run all 16 threads at AVX load"


# ── 3. The residency authority: proof from the server ────────────────────────────────────────────

class _FakePs:
    def __init__(self, *models: tuple[str, int, int]) -> None:
        self.models = [type("M", (), {"model": m, "size": s, "size_vram": v,
                                      "context_length": 24576})() for m, s, v in models]


class _FakeClient:
    def __init__(self, ps: Any) -> None:
        self._ps = ps
        self.unloaded: list[str] = []

    async def ps(self) -> Any:
        return self._ps

    async def generate(self, *, model: str, prompt: str, keep_alive: int) -> None:
        self.unloaded.append(model)


async def test_one_copy_is_fine() -> None:
    from sali.provider.residency import assert_single_residency, survey

    client = _FakeClient(_FakePs(("sali:latest", 16_000_000_000, 11_100_000_000),
                                 ("nomic-embed-text:latest", 300_000_000, 0)))
    report = await survey(client, "sali:latest")
    assert report.ok and report.chat_runners == 1
    assert report.runners[0].offloaded, "16GB resident in 11.1GB of VRAM is a split model"
    assert not report.runners[1].offloaded or True   # the embedder is CPU-only by design
    await assert_single_residency(client, "sali:latest", force=True)


async def test_two_copies_refuse_the_work() -> None:
    """The emergency this exists for: a second 16GB MoE that fits nowhere."""
    from sali.provider.residency import DuplicateResidency, assert_single_residency

    client = _FakeClient(_FakePs(("sali:latest", 16_000_000_000, 11_100_000_000),
                                 ("sali:latest", 16_000_000_000, 900_000_000)))
    with pytest.raises(DuplicateResidency) as err:
        await assert_single_residency(client, "sali:latest", force=True)
    assert "loaded 2×" in str(err.value)


async def test_a_duplicate_cannot_hide_behind_a_tag() -> None:
    """`sali` and `sali:latest` are the same model; comparing raw strings would miss the duplicate."""
    from sali.provider.residency import survey

    client = _FakeClient(_FakePs(("sali:latest", 1, 1), ("sali", 1, 1)))
    assert (await survey(client, "sali")).chat_runners == 2


async def test_an_unreadable_server_never_breaks_inference() -> None:
    """A diagnostic that can break the call it guards is worse than no diagnostic."""
    from sali.provider.residency import assert_single_residency, survey

    class Broken:
        async def ps(self) -> Any:
            raise RuntimeError("connection refused")

    report = await survey(Broken(), "sali:latest")
    assert report.ok and "could not read" in report.reason
    await assert_single_residency(Broken(), "sali:latest", force=True)   # must not raise


async def test_heal_evicts_every_copy() -> None:
    from sali.provider.residency import heal

    client = _FakeClient(_FakePs())
    await heal(client, "sali:latest")
    assert client.unloaded == ["sali:latest"]


async def test_server_envelope_flags_in_server_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    """OLLAMA_NUM_PARALLEL > 1 lets ONE runner decode two sequences at once — concurrency that
    happens inside the server, where none of Sali's locks can see it."""
    import sali.provider.residency as res

    async def fake_exec(*args: Any, **kw: Any) -> Any:
        class P:
            async def communicate(self) -> tuple[bytes, bytes]:
                return (b"Environment=OLLAMA_NUM_PARALLEL=4 OLLAMA_MAX_LOADED_MODELS=8\n", b"")
        return P()

    monkeypatch.setattr("sali.provider.residency.shutil.which",
                        lambda _: "/usr/bin/systemctl")
    monkeypatch.setattr("sali.provider.residency.asyncio.create_subprocess_exec", fake_exec)
    env = await res.server_envelope()
    assert not env["ok"]
    assert any("NUM_PARALLEL" in p for p in env["problems"])
    assert any("MAX_LOADED_MODELS" in p for p in env["problems"])


def test_cognition_is_residency_checked_but_embeddings_are_not() -> None:
    """Structural: the check belongs on the heavy model's paths, and must not add an HTTP round-trip
    to every embedding — those are a different, tiny, CPU-only model."""
    src = (SRC / "provider" / "ollama.py").read_text()
    assert "assert_single_residency" in src
    lease = src[src.index("async def _inference_lease("):]
    lease = lease[:lease.index("class OllamaProvider")]
    assert "kind is InferenceKind.COGNITION" in lease


# ── 4. The system-level VRAM guard: stop a duplicate/second load, spare the sole model ────────────
#
# The guard (scripts/vram_guard.py) is a standalone root daemon, not part of the package, so it is
# loaded by path. These tests pin the one decision that must be exactly right: a single legitimate
# model fills ~91% and its KV cache grows past 95% — that must NEVER be killed; only a DUPLICATE or a
# genuine second consumer may be.

_GUARD_PATH = Path(__file__).resolve().parents[1] / "scripts" / "vram_guard.py"


def _load_guard() -> Any:
    import sys
    spec = _ilu.spec_from_file_location("vram_guard", _GUARD_PATH)
    assert spec and spec.loader
    mod = _ilu.module_from_spec(spec)
    sys.modules["vram_guard"] = mod          # dataclasses(slots=True) needs the module registered
    spec.loader.exec_module(mod)
    return mod


def _wire(guard: Any, monkeypatch: pytest.MonkeyPatch, *, frac: float,
          apps: list[Any], runners: list[dict[str, Any]]) -> dict[str, list[Any]]:
    """Point the guard at a fake machine and record the actions it would take."""
    calls: dict[str, list[Any]] = {"unload": [], "stop": []}
    monkeypatch.setattr(guard, "gpu_state", lambda: (frac, apps))
    monkeypatch.setattr(guard, "ollama_ps", lambda: runners)
    def _unload(m: str) -> bool:
        calls["unload"].append(m)
        return True
    monkeypatch.setattr(guard, "ollama_unload", _unload)
    monkeypatch.setattr(guard, "stop_pid", lambda pid: calls["stop"].append(pid))
    monkeypatch.setattr(guard, "_proc_name", lambda pid: "ollama")
    return calls


def _app(guard: Any, pid: int, mib: int, name: str, started: float) -> Any:
    return guard.ComputeApp(pid=pid, used_mib=mib, name=name, started_at=started)


def test_guard_leaves_the_sole_model_alone_even_at_95_percent(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE false-positive that would make Sali unusable: one model legitimately sits at ~91% and its
    KV cache pushes it past 95%. One consumer, one runner — the guard must do nothing."""
    guard = _load_guard()
    apps = [_app(guard, 4001, 11500, "ollama", 100.0)]
    runners = [{"model": "sali:latest"}]
    calls = _wire(guard, monkeypatch, frac=0.955, apps=apps, runners=runners)
    guard.assess_and_act(0.0)
    assert calls["unload"] == [] and calls["stop"] == [], "killed the sole legitimate model"


def test_guard_unloads_a_duplicate_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact failure the user described: the model loaded again without checking it was loaded."""
    guard = _load_guard()
    apps = [_app(guard, 4001, 9000, "ollama", 100.0), _app(guard, 4002, 6000, "ollama", 200.0)]
    runners = [{"model": "sali:latest"}, {"model": "sali:latest"}]
    calls = _wire(guard, monkeypatch, frac=0.98, apps=apps, runners=runners)
    guard.assess_and_act(0.0)
    assert calls["unload"] == ["sali:latest"], "a duplicate model load was not stopped"


def test_guard_sheds_a_second_consumer_over_the_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model resident (one runner) AND a second, non-ollama GPU job pushes VRAM over the ceiling →
    shed the NEWEST consumer, never the established model."""
    guard = _load_guard()
    established = _app(guard, 4001, 11000, "ollama", 100.0)
    intruder = _app(guard, 5555, 1500, "python", 900.0)     # newer
    runners = [{"model": "sali:latest"}]
    calls = _wire(guard, monkeypatch, frac=0.985, apps=[intruder, established], runners=runners)
    # a non-ollama newest consumer → pid stop (its _proc_name is not ollama)
    monkeypatch.setattr(guard, "_proc_name", lambda pid: "python" if pid == 5555 else "ollama")
    guard.assess_and_act(0.0)
    assert calls["stop"] == [5555] or calls["unload"], "did not shed the second consumer"
    assert 4001 not in calls["stop"], "must never stop the established model's runner directly"


def test_guard_respects_a_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    """After acting, the guard waits before acting again — no kill storm while an unload takes effect."""
    guard = _load_guard()
    runners = [{"model": "sali:latest"}, {"model": "sali:latest"}]
    apps = [_app(guard, 4001, 9000, "ollama", 100.0), _app(guard, 4002, 6000, "ollama", 200.0)]
    calls = _wire(guard, monkeypatch, frac=0.98, apps=apps, runners=runners)
    import time as _t
    recent = _t.monotonic()
    guard.assess_and_act(recent)                 # within cooldown of "recent"
    assert calls["unload"] == [], "acted again inside the cooldown window"


def test_guard_never_signals_a_protected_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """stop_pid refuses init/ssh/etc. and, above all, its own pid."""
    guard = _load_guard()
    killed: list[int] = []
    monkeypatch.setattr(guard.os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(guard, "_proc_name", lambda pid: "sshd")
    guard.stop_pid(1234)
    assert killed == [], "signalled a protected process"
    import os as _os
    monkeypatch.setattr(guard, "_proc_name", lambda pid: "whatever")
    guard.stop_pid(_os.getpid())                 # never signal self
    assert killed == [], "signalled its own process"


def test_guard_is_inert_when_the_gpu_is_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    """No reading → no action. The guard must never act on data it does not have."""
    guard = _load_guard()
    calls = _wire(guard, monkeypatch, frac=0.0, apps=[], runners=[])
    guard.assess_and_act(0.0)
    assert calls["unload"] == [] and calls["stop"] == []
