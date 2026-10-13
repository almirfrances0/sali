"""APNs sender — the host half of iPhone push (§11).

The app already registers a token (`POST /devices/{id}/push-token`, devices.py). This is the piece
that USES it: when Sali says something on its own initiative, the phone gets a banner even though the
app is closed and its WebSocket is gone.

Push is a DELIVERY path, never a control path (§6). The durable event table remains the source of
truth and the socket remains the live channel; a push is a courtesy copy of something already
recorded, so every failure here is logged and swallowed. Nothing Sali does may depend on Apple.

What it needs to work — all absent by default, and honestly reported as such rather than pretended:

  1. An APNs auth key (.p8) from the Apple Developer portal, its 10-char Key ID, and the Team ID.
     The key is a credential: it belongs in the encrypted vault (`sali secrets set apns.key`), not in
     the repo and not in a plaintext config (§21/§22). A file path is accepted as an alternative for
     a key that already lives outside Sali.
  2. HTTP/2. APNs speaks nothing else, and httpx needs the `h2` package for it
     (`pip install ".[push]"`). Without it this sender says so once and stays inert.
  3. A device that has actually registered a token. `api_device.push_environment` records WHICH APNs
     environment issued it — a sandbox (debug-build) token is rejected outright by the production
     host and vice versa, so each device is pushed on the host its own token came from.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from sali.obs.log import get_logger

log = get_logger("sali.api.push")

# Two entirely separate APNs environments. `sandbox` tokens come from a build signed with
# `aps-environment = development`; `production` from a distribution build. Mixing them is a hard
# rejection (BadDeviceToken), not a degraded delivery — hence the per-device routing.
_HOSTS = {
    "sandbox": "https://api.sandbox.push.apple.com",
    "production": "https://api.push.apple.com",
}

# Apple accepts a provider token for up to an hour and rejects one older than that. Re-signing is
# cheap (one ECDSA op), so it happens well inside the window rather than at the edge of it.
_TOKEN_TTL_S = 45 * 60

# Reasons that mean "this token is dead" rather than "this attempt failed". Anything else is a
# transient or a configuration problem, and MUST NOT cost the device its registration.
_DEAD_TOKEN_REASONS = frozenset({"Unregistered", "BadDeviceToken", "DeviceTokenNotForTopic"})

# How long a banner may cost the process that is delivering the event. `on_event` is called from
# inside event fan-out, and the first connection to Apple has been observed stalling for >30s on a
# cold resolver — long enough to hold up every other subscriber behind it. A courtesy copy gets a
# budget; when it runs out the push is abandoned, never the event.
_ON_EVENT_BUDGET_S = 15.0

# `agent.message` carries an importance the app uses to filter (progress / update / milestone /
# question / warning / completion / failure). `progress` fires constantly inside a running turn — it
# is texture for a screen that is already open, and a banner for each one would be spam.
_QUIET_IMPORTANCES = frozenset({"progress"})


class ApnsUnavailable(RuntimeError):
    """Push cannot be attempted, and this says exactly why. Carries no secret."""


@dataclass(frozen=True, slots=True)
class ApnsCredentials:
    """Everything needed to sign a provider token. `private_key_pem` is a secret: it is never
    logged, never returned in an API response, and never written to Postgres."""

    team_id: str
    key_id: str
    private_key_pem: str
    topic: str

    @classmethod
    def resolve(cls, settings: Any, *, secrets: Any = None) -> ApnsCredentials:
        """Assemble credentials from settings + the secret store, or raise `ApnsUnavailable` naming
        the one thing that is missing. Never guesses a default for a credential."""
        push = getattr(settings, "push", None)
        if push is None or not push.enabled:
            raise ApnsUnavailable("push.enabled is false")
        if not push.team_id:
            raise ApnsUnavailable("push.team_id is not set (Apple Developer Team ID)")
        if not push.key_id:
            raise ApnsUnavailable("push.key_id is not set (the .p8 key's 10-character Key ID)")
        if not push.topic:
            raise ApnsUnavailable("push.topic is not set (the app's bundle identifier)")

        pem = _read_key(push, secrets)
        return cls(team_id=push.team_id, key_id=push.key_id, private_key_pem=pem, topic=push.topic)


def _read_key(push: Any, secrets: Any) -> str:
    """The .p8 itself — from the encrypted vault by preference, from a file only if pointed at one."""
    if push.key_ref:
        if secrets is None:
            from sali.config.secrets import SecretStore
            secrets = SecretStore()
        value = secrets.get(push.key_ref)
        if value:
            return str(value)
    if push.key_path:
        path = Path(push.key_path).expanduser()
        if path.is_file():
            return path.read_text()
        raise ApnsUnavailable(f"push.key_path points at nothing readable: {path}")
    raise ApnsUnavailable(
        f"no APNs key — store the .p8 with `sali secrets set {push.key_ref}` or set push.key_path")


def provider_token(creds: ApnsCredentials, *, issued_at: float | None = None) -> str:
    """The signed JWT APNs wants in `authorization: bearer …` (ES256 over the .p8 key).

    Hand-rolled rather than pulled from a JWT library for one reason: `cryptography` is already a
    dependency, and this is a 3-line JWS. The one subtlety is the signature encoding — `cryptography`
    emits DER, JWS requires the raw r‖s pair, and APNs answers a DER signature with a flat
    InvalidProviderToken that says nothing about why.
    """
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric import utils as asym_utils

    try:
        key = serialization.load_pem_private_key(creds.private_key_pem.encode(), password=None)
    except (ValueError, TypeError) as exc:
        raise ApnsUnavailable(f"the APNs key is not a readable PEM private key ({type(exc).__name__})") from exc
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        raise ApnsUnavailable("the APNs key must be an ES256 (P-256) key — that is what a .p8 is")

    header = _b64url(json.dumps({"alg": "ES256", "kid": creds.key_id}, separators=(",", ":")).encode())
    claims = _b64url(json.dumps(
        {"iss": creds.team_id, "iat": int(issued_at if issued_at is not None else time.time())},
        separators=(",", ":")).encode())
    signing_input = f"{header}.{claims}".encode()

    der = key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = asym_utils.decode_dss_signature(der)
    raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{header}.{claims}.{_b64url(raw)}"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def build_payload(title: str, body: str, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """The APNs payload. Only what the person needs to read on the lock screen goes in `alert` —
    never chain-of-thought, never a tool transcript (§11/§36); the custom keys carry only ids, so a
    tap can be routed to the subject the notification is about."""
    payload: dict[str, Any] = {
        "aps": {
            "alert": {"title": title, "body": body},
            "sound": "default",
            "thread-id": "sali",
        }
    }
    for key, value in (extra or {}).items():
        if value is not None:
            payload[key] = str(value)
    return payload


@dataclass(slots=True)
class PushResult:
    """What one fan-out actually did. Counts, never tokens."""

    sent: int = 0
    failed: int = 0
    pruned: int = 0
    reasons: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.reasons is None:
            self.reasons = []


class ApnsSender:
    """Sends one notification to every active device that has registered a token.

    Also an `EventSubscriber` (`on_event`): subscribed to the publisher, it turns Sali's own
    `agent.message` events into banners. That subscription is how the API process learns about
    messages published by ANY Sali process — the EventBridge delivers cross-process events to the
    same subscriber list.
    """

    def __init__(self, pool: Any, creds: ApnsCredentials, *, client: Any = None) -> None:
        self._pool = pool
        self._creds = creds
        self._client = client
        self._token: str | None = None
        self._token_at: float = 0.0
        self._lock = asyncio.Lock()

    # ── The event hook ────────────────────────────────────────────────────────────────────────────

    async def on_event(self, event: Any) -> None:
        """Best-effort by contract: this runs inside event delivery, so it may never raise and may
        never block the socket for long. One notification per agent-originated message."""
        if event.event_type != "agent.message":
            return
        data = event.data or {}
        importance = str(data.get("importance") or "update")
        if importance in _QUIET_IMPORTANCES:
            return
        text = str(data.get("text") or "").strip()
        if not text:
            return
        try:
            await asyncio.wait_for(
                self.send(title="Sali", body=text, extra={
                    "sali_event_type": event.event_type,
                    "sali_event_id": event.event_id,
                    "sali_task_id": event.task_id,
                }),
                timeout=_ON_EVENT_BUDGET_S)
        except TimeoutError:
            log.warning("apns_push_timeout", budget_s=_ON_EVENT_BUDGET_S, importance=importance)
        except Exception as exc:  # noqa: BLE001 - a courtesy copy must never break delivery
            log.warning("apns_push_failed", error=str(exc), importance=importance)

    # ── Sending ───────────────────────────────────────────────────────────────────────────────────

    async def send(self, *, title: str, body: str,
                   extra: dict[str, Any] | None = None) -> PushResult:
        """Fan out to every registered device, each on the host its own token came from."""
        targets = await self._targets()
        if not targets:
            return PushResult()

        payload = build_payload(title, _trim(body), extra=extra)
        token = await self._provider_token()
        client = await self._http()

        # One device at a time, on purpose. Fanning these out concurrently deadlocked httpx while
        # the HTTP/2 connection was still being established — two streams opened on a cold
        # connection simply never completed, and this runs inside event delivery where that is
        # unaffordable. Sequential costs a round trip per device (a handful, on a personal system),
        # reuses the single connection Apple prefers, and cannot wedge.
        out = PushResult()
        for target in targets:
            try:
                status, reason = await self._send_one(client, token, target, payload)
            except Exception as exc:  # noqa: BLE001 - one unreachable device is not the others' problem
                out.failed += 1
                out.reasons.append(type(exc).__name__)
                continue
            if status == 200:
                out.sent += 1
                continue
            out.failed += 1
            out.reasons.append(reason or f"HTTP {status}")
            if reason in _DEAD_TOKEN_REASONS:
                await self._prune(target["id"], reason)
                out.pruned += 1
        log.info("apns_push", sent=out.sent, failed=out.failed, pruned=out.pruned)
        return out

    async def _send_one(self, client: Any, provider: str, target: dict[str, Any],
                        payload: dict[str, Any]) -> tuple[int, str | None]:
        env = target.get("environment") or "production"
        host = _HOSTS.get(env)
        if host is None:
            return 0, f"unknown push environment {env!r}"
        response = await client.post(
            f"{host}/3/device/{target['token']}",
            json=payload,
            headers={
                "authorization": f"bearer {provider}",
                "apns-topic": self._creds.topic,
                "apns-push-type": "alert",
                "apns-priority": "10",
                "apns-expiration": str(int(time.time()) + 3600),
            },
        )
        if response.status_code == 200:
            return 200, None
        reason: str | None = None
        with contextlib.suppress(Exception):
            reason = str(response.json().get("reason"))
        return response.status_code, reason

    # ── Plumbing ──────────────────────────────────────────────────────────────────────────────────

    async def _targets(self) -> list[dict[str, Any]]:
        from sali.api.devices import DeviceStore
        return await DeviceStore(self._pool).push_targets()

    async def _prune(self, device_id: UUID, reason: str) -> None:
        """Apple says this token is gone (the app was deleted, or the build environment changed).
        Clearing it stops a dead device from costing a request per message forever; the app
        re-registers on its next launch, so nothing is lost."""
        from sali.api.devices import DeviceStore
        with contextlib.suppress(Exception):
            await DeviceStore(self._pool).clear_push_token(device_id)
        log.info("apns_token_pruned", device_id=str(device_id)[:8], reason=reason)

    async def _provider_token(self) -> str:
        async with self._lock:
            if self._token is None or (time.time() - self._token_at) > _TOKEN_TTL_S:
                self._token = provider_token(self._creds)
                self._token_at = time.time()
            return self._token

    async def _http(self) -> Any:
        if self._client is None:
            import httpx
            try:
                # Per-phase, not one blanket number: a connect that hangs is the failure mode here
                # (Apple is reached over IPv6 through a resolver that can be cold), and it deserves
                # a shorter leash than a read from a host that has already answered.
                self._client = httpx.AsyncClient(
                    http2=True,
                    timeout=httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0))
            except ImportError as exc:  # httpx raises this when `h2` is absent
                raise ApnsUnavailable(
                    "APNs needs HTTP/2 — install the extra with `pip install \".[push]\"`") from exc
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()
            self._client = None


def _trim(text: str, limit: int = 240) -> str:
    """A lock-screen banner shows a couple of lines. Sending a 2000-character message wastes the
    payload budget and reads as truncated garbage; this cuts it where a person can still read it."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"
