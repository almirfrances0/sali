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
from pathlib import Path

from sali.config.secrets import SecretStore

SUDO_REF = "sudo.password"
_ASKPASS_DIR = Path.home() / ".local" / "share" / "sali"
_ASKPASS_PATH = _ASKPASS_DIR / "sudo-askpass"
# Rewrite a `sudo` command word to use askpass — but not if it already has -A / --askpass.
_SUDO_RE = re.compile(r"\bsudo\b(?!\s+(?:-A\b|--askpass\b))")


def read_sudo_password(store: SecretStore | None = None) -> str | None:
    """The sudo password from the vault, or None. Used ONLY by the askpass helper (→ sudo's stdin)."""
    return (store or SecretStore()).get(SUDO_REF)


def sudo_configured(store: SecretStore | None = None) -> bool:
    try:
        return (store or SecretStore()).has(SUDO_REF)
    except Exception:  # noqa: BLE001 - an unreadable vault means "not configured", never a crash
        return False


def askpass_script() -> Path:
    """Path to the askpass helper, written once. It just execs `sali sudo-askpass`, which prints the
    vault password to stdout — 0700 so only Almir can run it."""
    if not _ASKPASS_PATH.exists():
        _ASKPASS_DIR.mkdir(parents=True, exist_ok=True)
        _ASKPASS_PATH.write_text("#!/bin/sh\nexec sali sudo-askpass\n", encoding="utf-8")
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
    return _SUDO_RE.sub("sudo --askpass", command)
