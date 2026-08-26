"""SecretStore — the one place Sali resolves a secret, and it NEVER touches Postgres (golden rule).

Secrets (SSH is delegated to ssh-agent; this is for email/CalDAV app-passwords, site logins) are
referenced by a dotted key like "mail.personal.password". Resolution order:
  1. env  SALI_SECRET__<KEY>   (KEY = ref upper-cased, '.'→'_' — matches the SALI_ settings convention)
  2. ~/.config/sali/secrets.toml   (flat quoted keys; REFUSED unless 0600, so an app-password can't
     sit group/other-readable)
  3. the OS keyring, if the optional `keyring` package is importable.
The value is never logged, never persisted to the DB, and redact_obj already masks it at every
boundary. Postgres holds only non-secret inventory plus the ref name (a pointer), never the value.
"""

from __future__ import annotations

import logging
import os
import stat
import tomllib
from pathlib import Path

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
    """Resolves secrets from env → 0600 secrets.toml → keyring. Constructed once and shared."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _SECRETS_PATH

    def get(self, ref: str) -> str | None:
        env = os.environ.get(_ENV_PREFIX + _env_key(ref))
        if env is not None:
            return env
        from_file = self._from_file(ref)
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
        """Write a secret into ~/.config/sali/secrets.toml with 0600 (dir 0700). CLI use only."""
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = self._load_file() or {}
        data[ref] = value
        body = "".join(f'"{k}" = "{_toml_escape(str(v))}"\n' for k, v in sorted(data.items()))
        fd = os.open(self._path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.chmod(self._path, 0o600)  # in case the file pre-existed with looser perms

    def refs(self) -> list[str]:
        """The ref NAMES configured in the file (never values) — for `sali secrets list`."""
        return sorted((self._load_file() or {}).keys())

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


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
