"""The sudo privilege broker (spec §28/§30).

Sali can perform authorized elevated operations WITHOUT ever holding Almir's sudo password as ordinary
text. The password lives only in the encrypted vault; when a command uses sudo, the broker points
sudo at an ASKPASS helper (`sali sudo-askpass`) that reads the vault and prints the password straight
to sudo — so the secret flows vault → helper stdout → sudo, and NEVER enters the model, the prompt, the
tool plan, the logs, or memory (§28). The model only ever asks to run `sudo <cmd>`; it never sees the
secret. If no sudo password is configured, nothing is rewritten — sudo simply fails and Sali reports it.
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

from sali.config.secrets import SecretStore

SUDO_REF = "sudo.password"
_ASKPASS_DIR = Path.home() / ".local" / "share" / "sali"
_ASKPASS_PATH = _ASKPASS_DIR / "sudo-askpass"
# Rewrite a `sudo` command word to use askpass — but not if it already has -A / --askpass.
#
# ANCHORED TO COMMAND POSITION. This used to match \bsudo\b anywhere in the string, so every command
# that merely MENTIONED sudo was corrupted: `grep sudo /var/log/auth.log` became
# `grep sudo --askpass /var/log/auth.log` ("grep: unrecognized option"), and `apt install sudo`,
# `ls /var/lib/sudo` and `usermod -aG sudo user` all broke the same way. A word is the command only
# at the start of the string or after a shell separator, so that is the only place it is rewritten.
_SUDO_RE = re.compile(
    r"(?:(?<=^)|(?<=[;&|(\n]))(\s*)sudo\b(?!\s+(?:-A\b|--askpass\b))")


def read_sudo_password(store: SecretStore | None = None) -> str | None:
    """The sudo password from the vault, or None. Used ONLY by the askpass helper (→ sudo's stdin)."""
    return (store or SecretStore()).get(SUDO_REF)


def sudo_configured(store: SecretStore | None = None) -> bool:
    try:
        return (store or SecretStore()).has(SUDO_REF)
    except Exception:  # noqa: BLE001 - an unreadable vault means "not configured", never a crash
        return False


def _askpass_body() -> str:
    """The helper's contents, with an ABSOLUTE path to the sali entry point.

    It used to say `exec sali sudo-askpass`, relying on PATH. The daemon's PATH is
    /usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin and `sali` is in the venv, so sudo got
    `exec: sali: not found` and fell back to demanding a tty password. Resolve it from the running
    interpreter — the daemon and the CLI share the venv, so this is the same `sali` either way."""
    candidate = Path(sys.executable).with_name("sali")
    exe = str(candidate) if candidate.exists() else (shutil.which("sali") or "sali")
    return f"#!/bin/sh\nexec {exe} sudo-askpass\n"


def askpass_script() -> Path:
    """Path to the askpass helper. It execs `sali sudo-askpass`, which prints the vault password to
    stdout — 0700 so only Almir can run it.

    Rewritten whenever the CONTENT differs, not merely when the file is absent: a stale helper from an
    older install would otherwise outlive every fix, which is exactly what happened here."""
    want = _askpass_body()
    try:
        current = _ASKPASS_PATH.read_text(encoding="utf-8")
    except OSError:
        current = None
    if current != want:
        _ASKPASS_DIR.mkdir(parents=True, exist_ok=True)
        _ASKPASS_PATH.write_text(want, encoding="utf-8")
    _ASKPASS_PATH.chmod(0o700)
    return _ASKPASS_PATH


def sudo_env(base: dict[str, str], store: SecretStore | None = None) -> dict[str, str]:
    """Add SUDO_ASKPASS to a command environment iff a sudo password is configured; else leave it be."""
    if not sudo_configured(store):
        return base
    return {**base, "SUDO_ASKPASS": str(askpass_script())}


def wrap_sudo(command: str, store: SecretStore | None = None) -> str:
    """Rewrite `sudo` → `sudo --askpass` so sudo pulls the password from the vault (never a tty/the
    model). No-op if the command has no sudo or no password is configured."""
    if "sudo" not in command or not sudo_configured(store):
        return command
    return _SUDO_RE.sub(r"\1sudo --askpass", command)
