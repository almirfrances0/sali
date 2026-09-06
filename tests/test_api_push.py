"""APNs push — the host half of iPhone banners (§11).

Everything here is provable without Apple, which is the point: the parts that used to be "trust the
integration" are the parts that silently break. Covered:

- the provider token is a genuine ES256 JWS over the .p8, signed the way APNs demands (raw r‖s, not
  the DER `cryptography` hands back — APNs answers DER with a flat InvalidProviderToken)
- the payload carries only what belongs on a lock screen
- each device is pushed on the host its OWN token came from (a sandbox token is rejected outright by
  the production host)
- a token Apple calls dead is forgotten, and the device survives it — pruning a route is not
  revoking a device (§24)
- a transient failure never costs a device its registration
- an unconfigured install raises `ApnsUnavailable` naming the missing piece, instead of no-opping
"""

from __future__ import annotations

import base64
import contextlib
import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.asymmetric import utils as asym_utils

from sali.api.push import (
    ApnsCredentials,
    ApnsSender,
    ApnsUnavailable,
    build_payload,
    provider_token,
)
from sali.config.settings import PushSettings
from sali.events.publisher import SaliEvent

TOPIC = "com.salieno.sali"


# ── helpers ───────────────────────────────────────────────────────────────────────────────────────


def _p8() -> str:
    """A throwaway P-256 key in the shape Apple issues a .p8 in."""
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def _creds(pem: str | None = None) -> ApnsCredentials:
    return ApnsCredentials(team_id="TEAM123456", key_id="KEY1234567",
                           private_key_pem=pem or _p8(), topic=TOPIC)


def _unb64(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


class _FakeResponse:
    def __init__(self, status_code: int, reason: str | None = None) -> None:
        self.status_code = status_code
        self._reason = reason

    def json(self) -> dict[str, Any]:
        if self._reason is None:
            return {}
        return {"reason": self._reason}


class _FakeClient:
    """Stands in for httpx: records what would have gone to Apple, answers from a script."""

    def __init__(self, answers: dict[str, _FakeResponse]) -> None:
        self._answers = answers
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, *, json: dict[str, Any],  # noqa: A002 - httpx's own name
                   headers: dict[str, str]) -> _FakeResponse:
        self.calls.append({"url": url, "payload": json, "headers": headers})
        for token, response in self._answers.items():
            if url.endswith(token):
                return response
        return _FakeResponse(200)

    async def aclose(self) -> None:
        return None


class _FakeConn:
    def __init__(self, rows: list[dict[str, Any]], sql: list[str]) -> None:
        self._rows = rows
        self.sql = sql

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.sql.append(query)
        return self._rows

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.sql.append(query)
        return args[0] if args else None


class _FakePool:
    """Just enough asyncpg to drive DeviceStore's two statements."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.sql: list[str] = []

    def acquire(self) -> Any:
        @contextlib.asynccontextmanager
        async def _ctx() -> Any:
            yield _FakeConn(self._rows, self.sql)

        return _ctx()


def _device(token: str, environment: str) -> dict[str, Any]:
    return {"id": uuid4(), "push_token": token, "push_environment": environment}


# ── the provider token ────────────────────────────────────────────────────────────────────────────


def test_provider_token_is_a_verifiable_es256_jws() -> None:
    pem = _p8()
    token = provider_token(_creds(pem), issued_at=1_700_000_000)
    header_b64, claims_b64, signature_b64 = token.split(".")

    assert json.loads(_unb64(header_b64)) == {"alg": "ES256", "kid": "KEY1234567"}
    assert json.loads(_unb64(claims_b64)) == {"iss": "TEAM123456", "iat": 1_700_000_000}

    # JWS wants the raw r‖s pair — 64 bytes for P-256 — not a DER structure.
    raw = _unb64(signature_b64)
    assert len(raw) == 64

    public = serialization.load_pem_private_key(pem.encode(), password=None).public_key()
    der = asym_utils.encode_dss_signature(
        int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    public.verify(der, f"{header_b64}.{claims_b64}".encode(), ec.ECDSA(hashes.SHA256()))


def test_provider_token_refuses_a_key_that_is_not_a_p8() -> None:
    rsa_pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    with pytest.raises(ApnsUnavailable, match="ES256"):
        provider_token(_creds(rsa_pem))


def test_provider_token_refuses_junk_instead_of_signing_it() -> None:
    with pytest.raises(ApnsUnavailable, match="PEM"):
        provider_token(_creds("-----BEGIN PRIVATE KEY-----\nnope\n-----END PRIVATE KEY-----\n"))


# ── the payload ───────────────────────────────────────────────────────────────────────────────────


def test_payload_is_lock_screen_only() -> None:
    payload = build_payload("Sali", "Disk is back under 80%.",
                            extra={"sali_task_id": None, "sali_event_type": "agent.message"})
    assert payload["aps"]["alert"] == {"title": "Sali", "body": "Disk is back under 80%."}
    assert payload["aps"]["thread-id"] == "sali"
    # A key with nothing behind it is omitted, not sent as the string "None".
    assert "sali_task_id" not in payload
    assert payload["sali_event_type"] == "agent.message"


# ── configuration ─────────────────────────────────────────────────────────────────────────────────


def test_resolve_names_the_missing_piece() -> None:
    settings = SimpleNamespace(push=PushSettings())
    with pytest.raises(ApnsUnavailable, match="team_id"):
        ApnsCredentials.resolve(settings)


def test_resolve_respects_being_switched_off() -> None:
    settings = SimpleNamespace(push=PushSettings(enabled=False, team_id="T", key_id="K"))
    with pytest.raises(ApnsUnavailable, match="enabled"):
        ApnsCredentials.resolve(settings)


def test_resolve_reads_the_key_from_the_secret_store() -> None:
    pem = _p8()
    settings = SimpleNamespace(push=PushSettings(team_id="TEAM123456", key_id="KEY1234567"))
    creds = ApnsCredentials.resolve(settings, secrets=SimpleNamespace(get=lambda ref: pem))
    assert creds.private_key_pem == pem
    assert creds.topic == TOPIC


# ── the fan-out ───────────────────────────────────────────────────────────────────────────────────


async def test_each_device_is_pushed_on_its_own_host() -> None:
    pool = _FakePool([_device("aaa111", "sandbox"), _device("bbb222", "production")])
    client = _FakeClient({})
    sender = ApnsSender(pool, _creds(), client=client)

    result = await sender.send(title="Sali", body="two devices, two environments")

    assert (result.sent, result.failed, result.pruned) == (2, 0, 0)
    urls = sorted(call["url"] for call in client.calls)
    assert urls == ["https://api.push.apple.com/3/device/bbb222",
                    "https://api.sandbox.push.apple.com/3/device/aaa111"]
    for call in client.calls:
        assert call["headers"]["apns-topic"] == TOPIC
        assert call["headers"]["apns-push-type"] == "alert"
        assert call["headers"]["authorization"].startswith("bearer ")


async def test_a_dead_token_is_forgotten_but_the_device_survives() -> None:
    pool = _FakePool([_device("dead999", "sandbox")])
    client = _FakeClient({"dead999": _FakeResponse(410, "Unregistered")})
    sender = ApnsSender(pool, _creds(), client=client)

    result = await sender.send(title="Sali", body="gone")

    assert (result.sent, result.failed, result.pruned) == (0, 1, 1)
    assert result.reasons == ["Unregistered"]
    cleared = [q for q in pool.sql if "push_token = NULL" in q]
    assert len(cleared) == 1
    # Pruning a route is not revoking a device: nothing deletes or revokes the row.
    assert not [q for q in pool.sql if "DELETE" in q.upper() or "revoked" in q]


async def test_a_transient_failure_keeps_the_token() -> None:
    pool = _FakePool([_device("alive1", "production")])
    client = _FakeClient({"alive1": _FakeResponse(503, "ServiceUnavailable")})
    sender = ApnsSender(pool, _creds(), client=client)

    result = await sender.send(title="Sali", body="apple is having a moment")

    assert (result.sent, result.failed, result.pruned) == (0, 1, 0)
    assert not [q for q in pool.sql if "push_token = NULL" in q]


async def test_no_registered_device_costs_nothing() -> None:
    pool = _FakePool([])
    client = _FakeClient({})
    sender = ApnsSender(pool, _creds(), client=client)

    result = await sender.send(title="Sali", body="nobody home")

    assert (result.sent, result.failed, result.pruned) == (0, 0, 0)
    assert client.calls == []


async def test_a_long_message_is_cut_where_a_person_can_read_it() -> None:
    pool = _FakePool([_device("aaa111", "sandbox")])
    client = _FakeClient({})
    sender = ApnsSender(pool, _creds(), client=client)

    await sender.send(title="Sali", body="word " * 200)

    body = client.calls[0]["payload"]["aps"]["alert"]["body"]
    assert len(body) <= 240
    assert body.endswith("…")


# ── the event hook ────────────────────────────────────────────────────────────────────────────────


async def test_only_agent_messages_become_banners() -> None:
    pool = _FakePool([_device("aaa111", "sandbox")])
    client = _FakeClient({})
    sender = ApnsSender(pool, _creds(), client=client)

    await sender.on_event(SaliEvent(event_type="tool.started", data={"tool": "shell"}))
    await sender.on_event(SaliEvent(event_type="agent.token", data={"text": "streaming"}))
    assert client.calls == []

    await sender.on_event(SaliEvent(
        event_type="agent.message", origin="agent",
        data={"text": "The nightly backup finished.", "importance": "completion"}))
    assert len(client.calls) == 1
    assert client.calls[0]["payload"]["aps"]["alert"]["body"] == "The nightly backup finished."
    assert client.calls[0]["payload"]["sali_event_type"] == "agent.message"


async def test_progress_chatter_is_not_a_banner() -> None:
    pool = _FakePool([_device("aaa111", "sandbox")])
    client = _FakeClient({})
    sender = ApnsSender(pool, _creds(), client=client)

    await sender.on_event(SaliEvent(
        event_type="agent.message", data={"text": "step 3 of 9", "importance": "progress"}))
    await sender.on_event(SaliEvent(event_type="agent.message", data={"text": "   "}))

    assert client.calls == []


async def test_a_failing_apple_never_breaks_event_delivery() -> None:
    class _Exploding:
        async def post(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("connection reset")

        async def aclose(self) -> None:
            return None

    pool = _FakePool([_device("aaa111", "sandbox")])
    sender = ApnsSender(pool, _creds(), client=_Exploding())

    # on_event is called from inside event delivery — it must swallow, not propagate.
    await sender.on_event(SaliEvent(
        event_type="agent.message", data={"text": "does this reach anyone?"}))
