"""ONE PC · ONE SALI · ONE MIND — the invariant, pinned.

These tests are the reason the guarantee survives future development. They check the mechanism at the
level it actually operates: real OS processes for the file lock, a real PostgreSQL backend for the
advisory lock, real source-code structure for the architectural rules. A test that only asserted a
Python singleton would prove exactly the thing that was already true and still wrong.

Layout mirrors the layers of the guarantee:
  1. the machine-wide lock  (sali.core.mind)
  2. the inference authority (sali.provider.authority)
  3. the composition root    (sali.kernel)
  4. cognitive arbitration   (sali.runtime.coordinator)
  5. the API as a window     (sali.api.app)
  6. the datastore's vote    (sali.db.mind_lock)          [db]
  7. architectural guards    (source structure — catches the NEXT mistake, not this one)
"""

from __future__ import annotations

import ast
import asyncio
import os
import re
import signal
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sali.core.mind import (
    MindLock,
    ProcessRole,
    SecondMindError,
    default_lock_path,
    live_holder,
    mind_is_live,
    set_current_role,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "sali"
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def lock_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """An isolated mind lock, so tests never contend with a real Sali running on this machine."""
    p = tmp_path / "mind.lock"
    monkeypatch.setenv("SALI_MIND_LOCK", str(p))
    yield p
    set_current_role(ProcessRole.UNKNOWN)


def _child(script: str, lock: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "SALI_MIND_LOCK": str(lock), "PYTHONPATH": str(REPO / "src")}
    return subprocess.run([sys.executable, "-c", textwrap.dedent(script)],
                          capture_output=True, text=True, env=env, timeout=60, check=False)


# ── 1. The machine-wide lock ──────────────────────────────────────────────────────────────────────

def test_lock_excludes_a_second_process(lock_path: Path) -> None:
    """The core claim: a SECOND OS PROCESS cannot become the mind. Not a singleton — a real fork."""
    lock = MindLock()
    assert lock.acquire(role=ProcessRole.MIND, api_host="127.0.0.1", api_port=8080)
    try:
        r = _child("""
            from sali.core.mind import MindLock, live_holder
            print("acquired:", MindLock().acquire())
            h = live_holder()
            print("sees_pid:", h.pid if h else None)
            print("sees_api:", h.api_base if h else None)
        """, lock_path)
        assert "acquired: False" in r.stdout, r.stdout + r.stderr
        assert f"sees_pid: {os.getpid()}" in r.stdout      # it can SEE the mind…
        assert "sees_api: http://127.0.0.1:8080" in r.stdout  # …and where to reach it
    finally:
        lock.release()


def test_lock_is_released_when_the_holder_is_killed(lock_path: Path) -> None:
    """No stale mind lock can exist: SIGKILL leaves no chance to clean up, and the kernel does it.

    This is why ownership needs no heartbeat, no expiry and no reaper — unlike the execution_lease,
    which cannot observe process death and therefore needs a 300s timeout.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent("""
            import sys, time
            from sali.core.mind import MindLock
            assert MindLock().acquire()
            print("held", flush=True)
            time.sleep(300)
        """)],
        stdout=subprocess.PIPE, text=True,
        env={**os.environ, "SALI_MIND_LOCK": str(lock_path), "PYTHONPATH": str(REPO / "src")})
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "held"
        assert mind_is_live()                       # a live holder
        proc.send_signal(signal.SIGKILL)            # no cleanup possible
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:                     # pragma: no cover - only on an assertion failure
            proc.kill()
    assert not mind_is_live(), "a killed mind must leave no lock behind"
    assert MindLock().acquire(), "the next Sali must be able to start immediately"


def test_a_stale_record_without_a_holder_is_not_a_live_mind(lock_path: Path) -> None:
    """The file may outlive its writer (it lives in $HOME, not tmpfs). Liveness is flock, not bytes."""
    lock_path.write_text('{"pid": 999999, "hostname": "kali", "boot_id": "x", "started_at": 0.0}')
    assert live_holder() is None
    assert MindLock().acquire()


def test_lock_path_ignores_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: a path derived from $XDG_RUNTIME_DIR resolves DIFFERENTLY in a systemd system unit
    (no XDG_RUNTIME_DIR) than in a terminal — so the daemon and a terminal would each have taken
    'the' lock and both become the only Sali. The path must come from the passwd database."""
    monkeypatch.delenv("SALI_MIND_LOCK", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/12345")
    with_xdg = default_lock_path()
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert default_lock_path() == with_xdg
    monkeypatch.setenv("HOME", "/nonexistent-home")
    assert default_lock_path() == with_xdg, "a stray HOME= in a unit file must not split the lock"


def test_holder_survives_a_dropped_reference(lock_path: Path) -> None:
    """Garbage-collecting a MindLock must not hand this machine to a second mind: closing the fd
    would release the flock, so acquired fds are anchored for the process's life."""
    import gc
    assert MindLock().acquire()      # no reference kept at all
    gc.collect()
    assert MindLock().acquire() is False


# ── 2. The inference authority ────────────────────────────────────────────────────────────────────

def test_cognition_is_refused_beside_a_living_mind(lock_path: Path) -> None:
    """A utility process may not think while Sali is alive — that is what a second mind IS."""
    from sali.provider import authority
    from sali.provider.authority import InferenceKind, InferenceRefused

    holder = MindLock()
    assert holder.acquire(role=ProcessRole.MIND)
    try:
        set_current_role(ProcessRole.UTILITY)          # pretend to be a CLI utility in this process
        with pytest.raises(InferenceRefused) as err:
            authority.admit(InferenceKind.COGNITION)
        assert "not Sali's mind" in str(err.value)
        authority.admit(InferenceKind.EMBEDDING)       # data work stays available
        assert authority.explain()["cognition_allowed_here"] is False
    finally:
        holder.release()


def test_cognition_is_allowed_when_nobody_is_alive(lock_path: Path) -> None:
    """A lone process IS the only Sali — the invariant seen from the other side."""
    from sali.provider import authority
    from sali.provider.authority import InferenceKind

    set_current_role(ProcessRole.UTILITY)
    authority.admit(InferenceKind.COGNITION)           # no mind → nothing to be second to
    assert authority.explain()["cognition_allowed_here"] is True


def test_the_mind_may_always_think(lock_path: Path) -> None:
    from sali.provider import authority
    from sali.provider.authority import InferenceKind

    lock = MindLock()
    assert lock.acquire(role=ProcessRole.MIND)
    try:
        authority.admit(InferenceKind.COGNITION)
        assert authority.explain()["is_mind"] is True
    finally:
        lock.release()


async def test_the_gpu_lease_actually_crosses_process_boundaries() -> None:
    """The claim the whole GPU-safety design rests on, tested where it lives — between processes.

    Every existing lease test passes on the in-process semaphore alone, so deleting the flock would
    have shipped green while `sali daemon` and a terminal generated concurrently on a 12GB card.
    Here a REAL second process holds the flock; this one must wait rather than generate beside it.
    """
    from sali.provider.authority import InferenceKind
    from sali.provider.ollama import _LOCK_PATH, _inference_lease

    holder = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import fcntl, os, sys, time
            fd = os.open({_LOCK_PATH!r}, os.O_CREAT | os.O_RDWR, 0o666)
            fcntl.flock(fd, fcntl.LOCK_EX)
            print("holding", flush=True)
            time.sleep(float(sys.argv[1]))
        """), "1.0"],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "holding"
        start = asyncio.get_running_loop().time()
        async with _inference_lease(InferenceKind.COGNITION):
            waited = asyncio.get_running_loop().time() - start
        assert waited > 0.5, (
            f"acquired the machine-wide lease in {waited:.2f}s while another process held it — "
            "generations are NOT serialised across processes")
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_every_ollama_call_goes_through_the_inference_lease() -> None:
    """No hidden inference path: every request to the model server sits inside _inference_lease(),
    which is where BOTH the single-mind gate and the single-generation lock live."""
    src = (SRC / "provider" / "ollama.py").read_text()
    calls = [m.start() for m in re.finditer(r"await self\._client\.(chat|embed|generate)\(", src)]
    assert calls, "expected model calls to audit"
    for pos in calls:
        window = src[max(0, pos - 1200):pos]
        assert "_inference_lease(" in window, (
            f"a model call at offset {pos} is not inside an _inference_lease() block — "
            "that is an inference path outside the one mind")


# ── 3. The composition root ───────────────────────────────────────────────────────────────────────

async def test_a_utility_kernel_cannot_build_a_runtime(lock_path: Path) -> None:
    """`Kernel.create()` is for datastore work. The only door to an AgentRuntime is machine-wide
    ownership, so no future caller can quietly open a second mind by reaching for the kernel."""
    from sali.config.settings import DbSettings, ModelSettings, Settings
    from sali.kernel import Kernel

    kernel = Kernel.create(Settings(db=DbSettings(name="sali_test"),
                                    model=ModelSettings(provider="fake")))
    assert kernel.is_mind is False
    with pytest.raises(SecondMindError):
        await kernel.runtime()


async def test_become_mind_refuses_a_second_time(lock_path: Path) -> None:
    """The authoritative entry point fails SAFELY — no pool, no model, no half-built runtime."""
    from sali.config.settings import DbSettings, ModelSettings, Settings
    from sali.kernel import Kernel

    settings = Settings(db=DbSettings(name="sali_test"), model=ModelSettings(provider="fake"))
    first = MindLock()
    assert first.acquire(role=ProcessRole.MIND)
    try:
        with pytest.raises(SecondMindError):
            await Kernel.become_mind(settings)
    finally:
        first.release()


# ── 4. Cognitive arbitration: one thing at a time ─────────────────────────────────────────────────

class _RecordingLoop:
    """A stand-in AgentLoop that reports overlap — the property under test."""

    def __init__(self, delay: float = 0.05, steps: int = 4) -> None:
        self.delay, self.steps = delay, steps
        self.concurrent = 0
        self.max_concurrent = 0
        self.started: list[str] = []

    # Mirrors the real signature: the coordinator now tells the loop whether a turn is one Sali
    # gave HIMSELF, so its input is not recorded in the transcript as something Almir said.
    async def astream(self, message: str, session_id: Any = None, *, run_id: Any = None,
                      as_subagent: bool = False, internal: bool = False) -> Any:
        from sali.runtime.loop import LoopEvent
        self.started.append(message)
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            for i in range(self.steps):
                await asyncio.sleep(self.delay)
                yield LoopEvent("status", f"{message}:{i}", {})
            yield LoopEvent("final", f"done: {message}", {})
        finally:
            self.concurrent -= 1


def _coordinator(loop: Any) -> Any:
    from sali.core.ids import new_id
    from sali.runtime.coordinator import AgentRuntimeCoordinator
    c = AgentRuntimeCoordinator()
    c.set_agent_loop(loop)
    c.set_session_id(new_id())
    return c


async def test_foreground_and_background_never_think_at_once() -> None:
    """Regression: background work used to call AgentLoop.astream() directly, outside the
    coordinator, so a scheduled turn and Almir's turn drove the same loop — the same task store, the
    same context engine — concurrently. One slot now, whoever is asking."""
    from sali.runtime.coordinator import ExecutionOrigin

    loop = _RecordingLoop()
    c = _coordinator(loop)
    await asyncio.gather(
        c.submit_background("sali's own work"),
        c.submit("almir speaks", ExecutionOrigin.CLI),
    )
    assert loop.max_concurrent == 1, "two turns drove the AgentLoop at once"


async def test_background_yields_to_almir_mid_turn() -> None:
    """'Sali, stop that' must actually stop it — the background turn is cancelled, not queued behind."""
    from sali.runtime.coordinator import ExecutionOrigin

    loop = _RecordingLoop(delay=0.05, steps=40)     # ~2s of background work
    c = _coordinator(loop)
    bg = asyncio.create_task(c.submit_background("long background sweep"))
    await asyncio.sleep(0.15)                        # let it get going
    assert c.is_thinking

    fg = await c.submit("stop that", ExecutionOrigin.CLI, timeout=10)
    assert fg["status"] == "completed"
    result = await bg
    assert result["status"] in ("yielded", "deferred"), result
    assert loop.max_concurrent == 1


async def test_background_defers_while_foreground_is_pending() -> None:
    """Sali does not start his own work while Almir is waiting."""
    from sali.runtime.coordinator import ExecutionOrigin

    loop = _RecordingLoop(delay=0.05, steps=10)
    c = _coordinator(loop)
    fg = asyncio.create_task(c.submit("almir speaks", ExecutionOrigin.CLI, timeout=10))
    await asyncio.sleep(0.05)
    deferred = await c.submit_background("curiosity")
    assert deferred["status"] == "deferred"
    assert await fg
    assert loop.started == ["almir speaks"], "background work must not have started"


async def test_background_runs_when_nobody_is_waiting() -> None:
    loop = _RecordingLoop(delay=0.01, steps=2)
    c = _coordinator(loop)
    r = await c.submit_background("nightly consolidation")
    assert r["status"] == "completed"
    assert loop.started == ["nightly consolidation"]


# ── 5. The API is a window ────────────────────────────────────────────────────────────────────────

async def test_the_api_never_builds_a_runtime() -> None:
    """create_app() has no path to a Kernel/AgentRuntime/AgentLoop — the runtime must be injected."""
    from sali.api.app import create_app

    class ExplodingKernel:
        """Any attempt to build a mind from inside the API is a test failure."""
        settings = None
        provider = None

        async def runtime(self, **_: Any) -> Any:
            raise AssertionError("the API tried to construct a runtime")

        async def pool(self) -> Any:
            return None

    app = create_app(kernel=ExplodingKernel())
    async with app.router.lifespan_context(app):
        assert getattr(app.state, "runtime", None) is None


def test_api_module_cannot_reach_the_composition_root() -> None:
    """A structural check, not a behavioural one: if `sali.api` ever imports Kernel again, the
    injection contract has a bypass and the next refactor will use it."""
    for py in (SRC / "api").rglob("*.py"):
        imports = {n.module for n in ast.walk(ast.parse(py.read_text()))
                   if isinstance(n, ast.ImportFrom) and n.module}
        assert "sali.kernel" not in imports, f"{py} imports the composition root"
        assert not _construction_calls(py) & {"Kernel", "Kernel.create", "Kernel.become_mind",
                                              "AgentLoop", "AgentRuntime"}, f"{py} builds a mind"


# ── 6. The datastore's vote ───────────────────────────────────────────────────────────────────────

@pytest.mark.db
async def test_pg_advisory_lock_excludes_a_second_mind(db_available: bool) -> None:
    """Covers what a file lock cannot see: two processes that resolve DIFFERENT lock paths (a
    different uid, home, or container) still share one `sali` database — and one Sali."""
    if not db_available:
        pytest.skip("Postgres not reachable")
    from sali.config.settings import DbSettings, Settings
    from sali.db.mind_lock import DbMindLock

    settings = Settings(db=DbSettings(name="sali_test"))
    first, second = DbMindLock(settings), DbMindLock(settings)
    assert await first.acquire() is True
    try:
        assert await second.acquire() is False, "two minds took the datastore lock"
    finally:
        await first.release()
    assert await second.acquire() is True     # released → the next Sali may start
    await second.release()


@pytest.mark.db
async def test_pg_lock_is_visible_to_anyone(db_available: bool) -> None:
    """`sali status` reads the living mind out of pg_locks, with no cooperation from the process."""
    if not db_available:
        pytest.skip("Postgres not reachable")
    from sali.config.settings import DbSettings, Settings
    from sali.db.mind_lock import DbMindLock, current_db_mind
    from sali.db.pool import create_pool

    settings = Settings(db=DbSettings(name="sali_test"))
    lock = DbMindLock(settings)
    assert await lock.acquire()
    pool = await create_pool(settings)
    try:
        assert await current_db_mind(pool) is not None
        await lock.release()
        assert await current_db_mind(pool) is None
    finally:
        await pool.close()


# ── 7. Architectural guards: catching the NEXT mistake ────────────────────────────────────────────

# Where each authoritative construction is ALLOWED to happen. Adding a file here is a deliberate act
# that says "this is part of the one mind"; doing it accidentally is what these tests prevent.
_ALLOWED: dict[str, set[str]] = {
    "Kernel": {"kernel.py"},
    "Kernel.create": {"cli/main.py"},
    "Kernel.become_mind": {"kernel.py", "cli/main.py"},
    "AgentLoop": {"kernel.py"},
    "AgentRuntime": {"kernel.py"},
    "AgentRuntimeCoordinator": {"runtime/runtime.py"},
    "OllamaProvider": {"provider/registry.py"},
}


def _call_name(node: ast.AST) -> str | None:
    """The dotted name being CALLED, ignoring prose: 'Kernel.create', 'AgentLoop', … or None."""
    if not isinstance(node, ast.Call):
        return None
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        return f"{f.value.id}.{f.attr}"
    return None


def _construction_calls(py: Path) -> set[str]:
    return {n for n in (_call_name(x) for x in ast.walk(ast.parse(py.read_text()))) if n}


def test_no_new_construction_sites_for_the_mind() -> None:
    """The one mind is built in exactly one place. If a future change constructs a Kernel, an
    AgentLoop, an AgentRuntime, a coordinator or a concrete provider anywhere else, that is a second
    mind waiting for a second process — so it fails here, at review time, not at 3am on the GPU.

    AST, not grep: a docstring that *describes* the architecture must not trip the guard, and a call
    hidden behind an alias must not slip past it.
    """
    offences: list[str] = []
    for py in SRC.rglob("*.py"):
        rel = str(py.relative_to(SRC))
        for node in ast.walk(ast.parse(py.read_text())):
            name = _call_name(node)
            if name and rel not in _ALLOWED.get(name, set()) and name in _ALLOWED:
                offences.append(f"{rel}:{getattr(node, 'lineno', '?')}: constructs {name}")
    assert not offences, (
        "the one mind may only be constructed at its composition root:\n  " + "\n  ".join(offences))


def test_the_authoritative_entry_points_take_ownership_first() -> None:
    """Every command that becomes a Sali must go through Kernel.become_mind(), which takes the
    machine-wide locks BEFORE a pool, a runtime or a model exists."""
    text = (SRC / "cli" / "main.py").read_text()
    for command in ("async def _daemon(", "async def _scheduler("):
        i = text.index(command)
        body = text[i:i + 3000]
        assert "become_mind(" in body, f"{command.strip()} does not take machine-wide ownership"
    serve = text[text.index("def serve("):][:2500]
    assert "become_mind(" in serve, "`sali serve` does not take machine-wide ownership"


def test_the_terminal_attaches_before_it_becomes_a_mind() -> None:
    """`sali agent` is a window first. It must consult the mind lock (which also tells it WHERE to
    attach) rather than probing a hardcoded port — a daemon on another port would be invisible."""
    text = (SRC / "cli" / "main.py").read_text()
    body = text[text.index("async def _agent("):][:2500]
    attach = body.index("_attach_to_living_sali")
    become = body.index("become_mind")
    assert attach < become, "`sali agent` must try to attach before it becomes the mind"
    helper = text[text.index("async def _attach_to_living_sali"):][:1500]
    assert "live_holder()" in helper, "attachment must be driven by the mind lock, not a port probe"
