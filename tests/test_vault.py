"""The encrypted secret vault (sali3 §27-29): ciphertext at rest, key management, graceful degrade."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sali.config.vault import Vault, VaultError


def _vault(tmp: Path) -> Vault:
    return Vault(tmp / "vault.json", tmp / "vault.key")


def test_roundtrip(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    v.set("mail.password", "s3cr3t-app-pw")
    assert v.get("mail.password") == "s3cr3t-app-pw"
    assert v.has("mail.password")


def test_value_is_ciphertext_at_rest(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    v.set("k", "hunter2")
    on_disk = (tmp_path / "vault.json").read_text(encoding="utf-8")
    assert "hunter2" not in on_disk  # the value is encrypted, not plaintext


def test_key_and_vault_files_are_0600(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    v.set("k", "x")
    assert oct((tmp_path / "vault.key").stat().st_mode & 0o777) == "0o600"
    assert oct((tmp_path / "vault.json").stat().st_mode & 0o777) == "0o600"


def test_refs_lists_names_without_decrypting(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    v.set("b.two", "2")
    v.set("a.one", "1")
    assert v.refs() == ["a.one", "b.two"]


def test_delete(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    v.set("k", "v")
    assert v.delete("k") is True and v.get("k") is None
    assert v.delete("k") is False  # already gone


def test_missing_ref_returns_none(tmp_path: Path) -> None:
    assert _vault(tmp_path).get("nope") is None


def test_rotated_key_makes_old_token_a_miss_not_a_crash(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    v.set("k", "x")
    (tmp_path / "vault.key").unlink()  # a new key is auto-created; the old token no longer decrypts
    assert Vault(tmp_path / "vault.json", tmp_path / "vault.key").get("k") is None


def test_group_readable_vault_file_is_ignored(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    v.set("k", "x")
    os.chmod(tmp_path / "vault.json", 0o644)
    assert v.get("k") is None  # loose perms → treated as absent


def test_group_readable_key_is_refused_loudly(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    v.set("k", "x")
    os.chmod(tmp_path / "vault.key", 0o644)
    with pytest.raises(VaultError):
        v.set("k2", "y")  # a real vault fault surfaces, not a silent miss


def test_set_refuses_and_preserves_when_vault_is_loose_perms(tmp_path: Path) -> None:
    # THE critical regression: a write must never truncate a vault it couldn't safely read.
    v = _vault(tmp_path)
    v.set("a", "1")
    v.set("b", "2")
    os.chmod(tmp_path / "vault.json", 0o644)  # momentarily loose (a backup restore, a synced dotfile)
    with pytest.raises(VaultError):
        v.set("c", "3")  # must fail closed, NOT clobber
    os.chmod(tmp_path / "vault.json", 0o600)
    assert v.get("a") == "1" and v.get("b") == "2" and v.get("c") is None  # nothing lost


def test_set_refuses_and_preserves_when_vault_is_corrupt(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    v.set("a", "1")
    (tmp_path / "vault.json").write_text("{ this is not valid json", encoding="utf-8")
    os.chmod(tmp_path / "vault.json", 0o600)  # perms fine → exercise the corrupt branch
    with pytest.raises(VaultError):
        v.set("b", "2")
    assert (tmp_path / "vault.json").read_text(encoding="utf-8").startswith("{ this")  # untouched


def test_refs_raises_when_vault_is_unreadable(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    v.set("a", "1")
    os.chmod(tmp_path / "vault.json", 0o644)
    with pytest.raises(VaultError):
        v.refs()  # a caller listing secrets must hear about it, not get a misleading empty list


def test_get_degrades_on_malformed_env_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    v = _vault(tmp_path)
    v.set("a", "1")  # stored under the machine key file
    monkeypatch.setenv("SALI_VAULT_KEY", "not-a-valid-fernet-key")
    assert Vault(tmp_path / "vault.json", tmp_path / "vault.key").get("a") is None  # degrade, no crash


def test_write_is_atomic_no_leftover_temp(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    v.set("a", "1")
    v.set("b", "2")
    assert not list(tmp_path.glob(".vault-*.tmp"))  # temp swapped in via os.replace, none left behind
    import json

    assert set(json.loads((tmp_path / "vault.json").read_text(encoding="utf-8"))) == {"a", "b"}


def test_env_key_override_uses_no_key_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cryptography.fernet import Fernet

    monkeypatch.setenv("SALI_VAULT_KEY", Fernet.generate_key().decode())
    key_path = tmp_path / "never.key"
    v = Vault(tmp_path / "vault.json", key_path)
    v.set("k", "via-env")
    assert v.get("k") == "via-env"
    assert not key_path.exists()  # the env key was used; no key file written
