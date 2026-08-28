"""Post-condition probes — independently RE-OBSERVE reality after an effectful command, so Sali
verifies an *effect* instead of trusting a tool's own exit code (spec §8/§23). These are read-only
system queries (dpkg / systemctl / ss / which / a path check) and live in the tools layer so a Tool's
``verify()`` may use them (``verify/`` sits above ``tools`` and cannot be imported downward). Higher
layers get the same probes as composable strategies via ``sali.verify.engine`` (which re-exports these).

Each probe returns ``VerifyResult | None``: a ``VerifyResult`` when it actually checked reality (the
verdict is real — success OR failure), or ``None`` when it could not probe (the probe binary is absent,
timed out, or the target can't be determined) — in which case the caller keeps the exit-code signal
rather than fabricating a pass/fail it did not observe.
"""

from __future__ import annotations

import re
from pathlib import Path

from sali.tools.base import VerifyResult
from sali.tools.exec import CommandTimeout, run_argv


async def _run(argv: list[str], timeout: float = 8.0) -> tuple[int, str, str, bool]:
    """Run a read-only probe. The 4th value is ``ran``: False when the probe itself couldn't execute
    (binary missing / timeout) — meaning we have no opinion, not a negative verdict."""
    try:
        rc, out, err = await run_argv(argv, timeout=timeout)
        return rc, out.strip(), err.strip(), True
    except FileNotFoundError:
        return 127, "", "", False
    except CommandTimeout:
        return 124, "", "timeout", False


def exit_ok(returncode: int, *, detail: str = "") -> VerifyResult:
    """The honest fallback when no probe applies: verify by exit code alone."""
    if returncode == 0:
        return VerifyResult(True, detail or "exit 0")
    return VerifyResult(False, detail or f"non-zero exit {returncode}")


def output_present(*, ok: bool, has_output: bool, error: str | None) -> VerifyResult:
    """Read-only tools: confirm the tool returned something."""
    if ok and has_output:
        return VerifyResult(True, "output present")
    return VerifyResult(False, error or "no output produced")


# ── reality probes ────────────────────────────────────────────────────────────────────────────────
async def service_active(unit: str) -> VerifyResult | None:
    rc, out, _err, ran = await _run(["systemctl", "is-active", "--", unit])
    if not ran:
        return None
    state = out.splitlines()[0] if out else ("active" if rc == 0 else "unknown")
    return VerifyResult(state == "active", f"service {unit} is {state}")


async def service_inactive(unit: str) -> VerifyResult | None:
    v = await service_active(unit)
    if v is None:
        return None
    return VerifyResult(not v.success, v.detail)


async def package_installed(pkg: str) -> VerifyResult | None:
    rc, out, _err, ran = await _run(["dpkg-query", "-W", "-f=${Status}", "--", pkg])
    if not ran:
        return None
    ok = rc == 0 and "install ok installed" in out
    return VerifyResult(ok, f"package {pkg} " + ("is installed" if ok else "is not installed"))


async def pip_installed(pkg: str) -> VerifyResult | None:
    rc, _out, _err, ran = await _run(["python3", "-m", "pip", "show", pkg])
    if not ran:
        return None
    return VerifyResult(rc == 0, f"pip package {pkg} " + ("is installed" if rc == 0 else "is not installed"))


async def binary_available(name: str) -> VerifyResult | None:
    rc, out, _err, ran = await _run(["which", "--", name])
    if not ran:
        return None
    return VerifyResult(rc == 0 and bool(out), f"{name} " + ("is on PATH" if rc == 0 else "is not on PATH"))


async def port_listening(port: int) -> VerifyResult | None:
    rc, out, _err, ran = await _run(["ss", "-ltnH"])
    if not ran or rc != 0:
        return None
    listening = re.search(rf":{port}\b", out) is not None
    return VerifyResult(listening, f"port {port} " + ("is listening" if listening else "is not listening"))


def path_exists(path: str) -> VerifyResult | None:
    if any(c in path for c in "*$") or not (path.startswith("/") or path.startswith("~")):
        return None  # globs/vars/relative paths can't be resolved reliably here → no opinion
    exists = Path(path).expanduser().exists()
    return VerifyResult(exists, f"{path} " + ("exists" if exists else "does not exist"))


def path_absent(path: str) -> VerifyResult | None:
    v = path_exists(path)
    if v is None:
        return None
    return VerifyResult(not v.success, v.detail)


# ── command-shape router: pick the right reality probe for a shell command ──────────────────────────
def _unquote(token: str) -> str:
    return token.strip().strip("'\"")


async def verify_command(command: str) -> VerifyResult | None:
    """Given a shell command that just ran, independently verify its intended effect by shape.
    Covers the directive's named cases — install → package present, service control → status, a bound
    port → a listener — plus mkdir/touch → path exists and rm → path gone. Returns None when the shape
    is unrecognised or the effect can't be checked, so the caller falls back to the exit code."""
    core = re.sub(r"^\s*sudo\s+(?:-\S+\s+)*", "", command.strip())

    m = re.search(
        r"\bsystemctl\s+(?:--\w+\s+)*(start|restart|reload|enable|stop|disable)\b(?:\s+--now)?\s+([\w@.\-]+)",
        core,
    )
    if m:
        action, unit = m.group(1), m.group(2)
        return await (service_inactive(unit) if action in ("stop", "disable") else service_active(unit))

    m = re.search(r"\bapt(?:-get)?\s+(?:-\S+\s+)*install\s+(?:-\S+\s+)*([\w.\-+]+)", core)
    if m:
        return await package_installed(m.group(1))

    m = re.search(r"\bpip3?\s+install\s+(?:-\S+\s+)*([\w.\-]+)", core)
    if m:
        return await pip_installed(re.split(r"[\[=<>!~]", m.group(1))[0])

    m = re.search(r"(?:http\.server\s+|--port[ =]|(?<![\w-])-p\s+)(\d{2,5})\b", core)
    if m:
        return await port_listening(int(m.group(1)))

    m = re.search(r"\b(?:mkdir|touch)\s+(?:-\S+\s+)*(\S+)", core)
    if m:
        return path_exists(_unquote(m.group(1)))

    m = re.search(r"\brm\s+(?:-\S+\s+)*(\S+)", core)
    if m:
        return path_absent(_unquote(m.group(1)))

    return None
