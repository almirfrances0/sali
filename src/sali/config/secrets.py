"""SecretStore — the one place Sali resolves a secret, and it NEVER touches Postgres (golden rule).

Secrets (SSH is delegated to ssh-agent; this is for email/CalDAV app-passwords, site logins) are
referenced by a dotted key like "mail.personal.password". Resolution order:
  1. env  SALI_SECRET__<KEY>   (KEY = ref upper-cased, '.'→'_' — matches the SALI_ settings convention)
  2. the ENCRYPTED VAULT (~/.config/sali/vault.json) — where `set()` now writes, encrypted at rest.
  3. ~/.config/sali/secrets.toml   (legacy PLAINTEXT, read-only fallback; REFUSED unless 0600). Move
     it into the vault with `sali secrets migrate`, then delete it.
  4. the OS keyring, if the optional `keyring` package is importable.
The value is never logged, never persisted to the DB, and redact_obj already masks it at every
boundary. Postgres holds only non-secret inventory plus the ref name (a pointer), never the value.
"""

from __future__ import annotations

import logging
import os
import stat
import tomllib
from pathlib import Path

from sali.config.vault import Vault

log = logging.getLogger("sali.secrets")

_ENV_PREFIX = "SALI_SECRET__"
_SECRETS_PATH = Path.home() / ".config" / "sali" / "secrets.toml"


class SecretNotFound(KeyError):
    """A required secret isn't configured. Carries the ref NAME, never a value."""

    def __init__(self, ref: str) -> None:
        super().__init__(ref)
        self.ref = ref

    def __str__(self) -> str:
        return (f"secret '{self.ref}' is not set — add it via `sali secrets set {self.ref}` "
                f"or the {_ENV_PREFIX}{_env_key(self.ref)} env var")


def _env_key(ref: str) -> str:
    return ref.upper().replace(".", "_")


class SecretStore:
    """Resolves secrets from env → encrypted vault → legacy plaintext → keyring. `set()` writes to
    the encrypted vault. Constructed once and shared; inject a `vault` in tests for isolation."""

    def __init__(self, path: Path | None = None, vault: Vault | None = None) -> None:
        self._path = path or _SECRETS_PATH
        self._vault = vault if vault is not None else Vault()

    def get(self, ref: str) -> str | None:
        env = os.environ.get(_ENV_PREFIX + _env_key(ref))
        if env is not None:
            return env
        from_vault = self._vault.get(ref)
        if from_vault is not None:
            return from_vault
        from_file = self._from_file(ref)  # legacy plaintext, read-only fallback
        if from_file is not None:
            return from_file
        return self._from_keyring(ref)

    def require(self, ref: str) -> str:
        value = self.get(ref)
        if value is None:
            raise SecretNotFound(ref)
        return value

    def has(self, ref: str) -> bool:
        return self.get(ref) is not None

    def set(self, ref: str, value: str) -> None:
        """Store a secret ENCRYPTED at rest in the vault (~/.config/sali/vault.json). CLI use only."""
        self._vault.set(ref, value)

    def delete(self, ref: str) -> bool:
        """Remove a secret from the vault. Returns False if it wasn't there."""
        return self._vault.delete(ref)

    def refs(self) -> list[str]:
        """The ref NAMES configured (never values) — vault + any legacy plaintext, for `secrets list`."""
        legacy = set((self._load_file() or {}).keys())
        return sorted(set(self._vault.refs()) | legacy)

    def migrate_legacy(self) -> list[str]:
        """Move any legacy plaintext secrets into the encrypted vault (skipping refs already there).
        Returns the refs migrated. Leaves the plaintext file untouched — verify, then delete it."""
        migrated = []
        for ref, value in (self._load_file() or {}).items():
            if not self._vault.has(ref):
                self._vault.set(ref, str(value))
                migrated.append(ref)
        return sorted(migrated)

    def _from_file(self, ref: str) -> str | None:
        data = self._load_file()
        if data is None:
            return None
        value = data.get(ref)
        return str(value) if value is not None else None

    def _load_file(self) -> dict[str, object] | None:
        try:
            st = self._path.stat()
        except OSError:
            return None
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):  # group/other bits set → refuse
            log.warning("secrets file %s is group/other-accessible (needs 0600) — ignoring it",
                        self._path)
            return None
        try:
            with self._path.open("rb") as fh:
                return tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            log.warning("could not read secrets file: %s", exc)
            return None

    def _from_keyring(self, ref: str) -> str | None:
        try:
            import keyring  # optional third tier
        except ImportError:
            return None
        try:
            value = keyring.get_password("sali", ref)
        except Exception:  # noqa: BLE001 - a broken keyring backend must not crash a lookup
            return None
        return str(value) if value is not None else None


class FakeSecretStore:
    """In-memory secrets for CI — same surface as SecretStore, no filesystem, no env."""

    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        self._secrets = dict(secrets or {})

    def get(self, ref: str) -> str | None:
        return self._secrets.get(ref)

    def require(self, ref: str) -> str:
        if ref not in self._secrets:
            raise SecretNotFound(ref)
        return self._secrets[ref]

    def has(self, ref: str) -> bool:
        return ref in self._secrets

    def set(self, ref: str, value: str) -> None:
        self._secrets[ref] = value

    def refs(self) -> list[str]:
        return sorted(self._secrets.keys())

    def delete(self, ref: str) -> bool:
        return self._secrets.pop(ref, None) is not None
