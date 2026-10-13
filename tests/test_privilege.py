"""Phase 2 · Increment 7 — the sudo privilege broker (§28/§30).

Sali can run authorized sudo commands, but the password lives ONLY in the vault and reaches sudo via
SUDO_ASKPASS — it never appears in the command, the environment value, or anything the model sees.
"""

from __future__ import annotations

from pathlib import Path

from sali.config.secrets import SecretStore
from sali.config.vault import Vault
from sali.tools import privilege


def _store(tmp: Path, *, with_password: bool) -> SecretStore:
    store = SecretStore(path=tmp / "secrets.toml", vault=Vault(tmp / "vault.json", tmp / "vault.key"))
    if with_password:
        store.set(privilege.SUDO_REF, "masaka")
    return store


def test_wrap_sudo_routes_through_askpass_when_configured(tmp_path: Path) -> None:
    store = _store(tmp_path, with_password=True)
    assert privilege.wrap_sudo("sudo systemctl restart nginx", store) == \
        "sudo --askpass systemctl restart nginx"
    # mid-command sudo is handled too
    assert privilege.wrap_sudo("cd /srv && sudo make install", store) == \
        "cd /srv && sudo --askpass make install"


def test_wrap_sudo_is_idempotent_and_leaves_non_sudo_alone(tmp_path: Path) -> None:
    store = _store(tmp_path, with_password=True)
    assert privilege.wrap_sudo("sudo -A whoami", store) == "sudo -A whoami"          # already askpass
    assert privilege.wrap_sudo("sudo --askpass whoami", store) == "sudo --askpass whoami"
    assert privilege.wrap_sudo("ls -la", store) == "ls -la"                           # no sudo


def test_no_rewrite_without_a_configured_password(tmp_path: Path) -> None:
    store = _store(tmp_path, with_password=False)
    assert privilege.wrap_sudo("sudo reboot", store) == "sudo reboot"  # sudo will just fail → reported
    assert "SUDO_ASKPASS" not in privilege.sudo_env({"PATH": "/usr/bin"}, store)


def test_sudo_env_adds_askpass_but_never_the_secret(tmp_path: Path) -> None:
    store = _store(tmp_path, with_password=True)
    env = privilege.sudo_env({"PATH": "/usr/bin"}, store)
    assert "SUDO_ASKPASS" in env
    # the SECRET must never be in the environment we hand to the command — only a path to the helper
    assert all("masaka" not in v for v in env.values())
    assert env["SUDO_ASKPASS"].endswith("sudo-askpass")


def test_the_password_is_readable_only_through_the_broker(tmp_path: Path) -> None:
    store = _store(tmp_path, with_password=True)
    assert privilege.read_sudo_password(store) == "masaka"   # the askpass helper reads this → sudo
    # and the wrapped command NEVER contains the secret itself
    wrapped = privilege.wrap_sudo("sudo apt update", store)
    assert "masaka" not in wrapped
