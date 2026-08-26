"""SecretStore (the credential spine): resolution order, vault-backed set, legacy fallback, migrate."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sali.config.secrets import FakeSecretStore, SecretNotFound, SecretStore
from sali.config.vault import Vault, VaultError


def _store(tmp: Path) -> SecretStore:
    return SecretStore(path=tmp / "secrets.toml", vault=Vault(tmp / "vault.json", tmp / "vault.key"))


def _write_legacy(path: Path, data: dict[str, str], mode: int = 0o600) -> None:
    path.write_text("".join(f'"{k}" = "{v}"\n' for k, v in data.items()), encoding="utf-8")
    os.chmod(path, mode)


def test_env_takes_precedence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.set("mail.personal.password", "from-vault")
    monkeypatch.setenv("SALI_SECRET__MAIL_PERSONAL_PASSWORD", "from-env")
    assert store.get("mail.personal.password") == "from-env"  # env wins over the vault


def test_vault_roundtrip_and_is_encrypted_at_rest(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.set("mail.personal.password", 'a "quoted" \\ secretpw')
    assert store.get("mail.personal.password") == 'a "quoted" \\ secretpw'
    assert store.refs() == ["mail.personal.password"]  # names only
    blob = (tmp_path / "vault.json").read_text(encoding="utf-8")
    assert "secretpw" not in blob and "quoted" not in blob  # never plaintext on disk


def test_legacy_plaintext_is_read_as_a_fallback(tmp_path: Path) -> None:
    _write_legacy(tmp_path / "secrets.toml", {"caldav.url": "https://dav.example/u/"})
    store = _store(tmp_path)
    assert store.get("caldav.url") == "https://dav.example/u/"  # still resolvable
    assert "caldav.url" in store.refs()


def test_group_readable_legacy_file_is_refused(tmp_path: Path) -> None:
    _write_legacy(tmp_path / "secrets.toml", {"x.y": "sensitive"}, mode=0o644)
    store = _store(tmp_path)
    assert store.get("x.y") is None  # world/group-readable plaintext → treated as absent


def test_migrate_moves_plaintext_into_the_encrypted_vault(tmp_path: Path) -> None:
    _write_legacy(tmp_path / "secrets.toml", {"a.b": "v1", "c.d": "v2"})
    store = _store(tmp_path)
    assert store.migrate_legacy() == ["a.b", "c.d"]
    assert store._vault.get("a.b") == "v1"  # now in the vault
    assert "v1" not in (tmp_path / "vault.json").read_text(encoding="utf-8")  # encrypted
    assert store.migrate_legacy() == []  # idempotent: already-present refs are skipped


def test_migrate_aborts_and_preserves_when_vault_unreadable(tmp_path: Path) -> None:
    # Fail closed: if the vault can't be read safely, migration must not clobber it or half-write.
    vault = Vault(tmp_path / "vault.json", tmp_path / "vault.key")
    vault.set("existing", "keepme")
    os.chmod(tmp_path / "vault.json", 0o644)  # momentarily loose
    _write_legacy(tmp_path / "secrets.toml", {"a.b": "v1"})
    store = SecretStore(path=tmp_path / "secrets.toml", vault=vault)
    with pytest.raises(VaultError):
        store.migrate_legacy()
    os.chmod(tmp_path / "vault.json", 0o600)
    assert vault.get("existing") == "keepme"  # untouched


def test_get_falls_through_to_legacy_when_vault_key_is_bad(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A ref that lives in BOTH the vault and legacy plaintext: a broken vault key must not hide it.
    store = _store(tmp_path)
    store.set("dup.ref", "from-vault")
    _write_legacy(tmp_path / "secrets.toml", {"dup.ref": "from-legacy"})
    monkeypatch.setenv("SALI_VAULT_KEY", "malformed-key")  # vault tier now degrades to a miss
    assert store.get("dup.ref") == "from-legacy"  # resolution falls through, no crash


def test_delete_removes_from_the_vault(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.set("k", "v")
    assert store.delete("k") is True and store.get("k") is None
    assert store.delete("k") is False


def test_require_raises_with_ref_not_value() -> None:
    store = FakeSecretStore({"present": "v"})
    assert store.require("present") == "v"
    with pytest.raises(SecretNotFound) as exc:
        store.require("mail.work.password")
    assert exc.value.ref == "mail.work.password"
    assert "mail.work.password" in str(exc.value)  # names the ref, to help configure it
    assert "secret" in str(exc.value).lower()


def test_fake_secret_store_surface() -> None:
    store = FakeSecretStore()
    assert not store.has("a")
    store.set("a", "b")
    assert store.has("a") and store.get("a") == "b" and store.refs() == ["a"]
    assert store.delete("a") is True and not store.has("a")
