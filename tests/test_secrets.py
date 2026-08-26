"""SecretStore (the credential spine): resolution order, 0600 enforcement, and the fake."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sali.config.secrets import FakeSecretStore, SecretNotFound, SecretStore


def test_env_takes_precedence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = SecretStore(path=tmp_path / "secrets.toml")
    store.set("mail.personal.password", "from-file")
    monkeypatch.setenv("SALI_SECRET__MAIL_PERSONAL_PASSWORD", "from-env")
    assert store.get("mail.personal.password") == "from-env"  # env wins over file


def test_file_roundtrip_and_is_written_0600(tmp_path: Path) -> None:
    path = tmp_path / "secrets.toml"
    store = SecretStore(path=path)
    store.set("caldav.url", "https://dav.example/user/calendars/")
    store.set("mail.personal.password", 'a "quoted" \\ pw')  # escaping round-trips
    assert store.get("caldav.url") == "https://dav.example/user/calendars/"
    assert store.get("mail.personal.password") == 'a "quoted" \\ pw'
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert store.refs() == ["caldav.url", "mail.personal.password"]  # names only


def test_group_readable_file_is_refused(tmp_path: Path) -> None:
    # A secrets file that others could read is treated as ABSENT (never read) — an app-password must
    # not sit world/group-readable.
    path = tmp_path / "secrets.toml"
    store = SecretStore(path=path)
    store.set("x.y", "sensitive")
    os.chmod(path, 0o644)  # loosen it behind the store's back
    assert store.get("x.y") is None  # refused


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
