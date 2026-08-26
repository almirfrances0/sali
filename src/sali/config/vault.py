"""The encrypted secret vault (sali3 §27-29) — secrets encrypted at rest, never plaintext on disk.

Each value is Fernet-encrypted (AES-128-CBC + HMAC, from `cryptography`) with a machine-local key;
the vault file holds only ciphertext keyed by the ref name (a pointer, not a secret). So a stray
backup, a synced dotfile, or a `grep` over the disk never yields a usable credential — only the key
file (0600, alongside) plus the vault together decrypt anything.

Key source, in order: the ``SALI_VAULT_KEY`` env var (a base64 Fernet key, for those who inject it
from a real secrets manager), else a machine-local key file auto-created 0600 on first use. This
key-file model is what lets Sali's always-on daemon read secrets without a human typing a passphrase.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger("sali.vault")

_VAULT_PATH = Path.home() / ".config" / "sali" / "vault.json"
_KEY_PATH = Path.home() / ".config" / "sali" / "vault.key"
_ENV_KEY = "SALI_VAULT_KEY"


class VaultError(RuntimeError):
    """The vault couldn't be opened (missing `cryptography`, unreadable key). Never carries a value."""


class Vault:
    """Encrypted-at-rest key→value secret storage. Constructed once and shared; cheap to build
    (the key and file are read lazily, per operation, so a rotated key is picked up)."""

    def __init__(self, vault_path: Path | None = None, key_path: Path | None = None) -> None:
        self._path = vault_path or _VAULT_PATH
        self._key_path = key_path or _KEY_PATH

    # ── public surface (mirrors SecretStore) ─────────────────────────────────

    def get(self, ref: str) -> str | None:
        try:
            data = self._read()
            token = data.get(ref)
            if token is None:
                return None
            fernet = self._fernet()  # missing crypto / loose or malformed key → VaultError
        except VaultError as exc:
            # A READ degrades to a miss (so SecretStore falls through to legacy/keyring) — never a crash.
            log.warning("vault unreadable for '%s': %s", ref, exc)
            return None
        try:
            return str(fernet.decrypt(token.encode()).decode())
        except Exception:  # noqa: BLE001 - a rotated/corrupt token is just a miss, not a crash
            log.warning("vault entry '%s' could not be decrypted (key rotated or corrupt)", ref)
            return None

    def has(self, ref: str) -> bool:
        return ref in self._read()  # propagates VaultError: an unreadable vault is not "empty"

    def set(self, ref: str, value: str) -> None:
        data = self._read()  # raises if the vault is present-but-unreadable → we REFUSE to clobber it
        data[ref] = self._fernet().encrypt(value.encode()).decode()
        self._write(data)

    def delete(self, ref: str) -> bool:
        data = self._read()  # raises rather than truncate a vault we couldn't read
        if ref not in data:
            return False
        del data[ref]
        self._write(data)
        return True

    def refs(self) -> list[str]:
        """The ref NAMES stored (never values, no decryption). Raises VaultError if the vault is
        present but unreadable — a caller listing secrets should hear that, not see an empty list."""
        return sorted(self._read().keys())

    # ── key management ───────────────────────────────────────────────────────

    def _fernet(self) -> Any:
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:  # cryptography is a core dep, but fail clearly if it's missing
            raise VaultError("the 'cryptography' package is required for the secret vault") from exc
        try:
            return Fernet(self._key())
        except (ValueError, TypeError) as exc:  # a malformed SALI_VAULT_KEY → a clear vault fault
            raise VaultError(f"vault key is invalid: {exc}") from exc

    def _key(self) -> bytes:
        env = os.environ.get(_ENV_KEY)
        if env:
            return env.encode()
        try:
            st = self._key_path.stat()
        except OSError:
            return self._create_key()
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):  # group/other can read the key → refuse
            raise VaultError(f"vault key {self._key_path} is group/other-accessible (needs 0600)")
        return self._key_path.read_bytes()

    def _create_key(self) -> bytes:
        from cryptography.fernet import Fernet

        key = Fernet.generate_key()
        self._key_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self._key_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        os.chmod(self._key_path, 0o600)
        return key

    # ── file I/O ─────────────────────────────────────────────────────────────

    def _read(self) -> dict[str, str]:
        """Stored ciphertext. Returns {} ONLY when the file is genuinely ABSENT; raises VaultError if
        the file is present but unreadable (loose perms, corrupt, malformed). This is the fix that
        keeps a write from silently truncating a vault it couldn't read (never lose a secret)."""
        try:
            st = self._path.stat()
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise VaultError(f"cannot stat vault {self._path}: {exc}") from exc
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):  # ciphertext, but a loose vault is not "empty"
            raise VaultError(f"vault file {self._path} is group/other-accessible (needs 0600)")
        try:
            data = json.loads(self._path.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError) as exc:
            raise VaultError(f"vault file {self._path} is unreadable or corrupt: {exc}") from exc
        if not isinstance(data, dict):
            raise VaultError(f"vault file {self._path} is malformed (not a JSON object)")
        return {str(k): str(v) for k, v in data.items()}

    def _write(self, data: dict[str, str]) -> None:
        # Atomic: write a sibling temp file (0600), fsync, then os.replace — a crash/ENOSPC mid-write
        # leaves either the old or the new COMPLETE vault, never a truncated one (never lose a secret).
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        body = json.dumps(data, indent=2, sort_keys=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".vault-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(body)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)  # atomic on the same filesystem
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
