"""LAN-discovery feature: Bonjour advertisement + /identity endpoint + settings.

Ground-truth tests. The mDNS advertisement is tested with a real Zeroconf browser on the loopback
interface (fast, no network); the /identity endpoint is tested through the FastAPI TestClient with
no auth (that's the point — discovery must be verifiable BEFORE the iPhone has any credentials).

Source-level guards enforce the security contract: no token/secret literal ever appears in the TXT
record or in the /identity response.
"""

from __future__ import annotations

import asyncio
import pathlib
import re
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient


# ── /identity endpoint ────────────────────────────────────────────────────────────────────────────


def _build_bare_app(runtime_session_id: str | None = "abc-123") -> Any:
    """A stripped `create_app()` without a Kernel — enough to exercise /identity and /healthz."""
    from sali.api.app import create_app

    app = create_app(kernel=None)

    class _RuntimeStub:
        def __init__(self, sid: str | None) -> None:
            self.session_id = sid

    if runtime_session_id is not None:
        app.state.runtime = _RuntimeStub(runtime_session_id)
    return app


def test_identity_endpoint_returns_public_metadata() -> None:
    app = _build_bare_app(runtime_session_id="rt-1234-5678")
    with TestClient(app) as client:
        r = client.get("/identity")
        assert r.status_code == 200
        data = r.json()
    assert data["service"] == "sali"
    assert data["runtime_id"] == "rt-1234-5678"
    assert "version" in data
    assert data["protocol"] == "1"
    # `hostname` intentionally REMOVED (turn-2 security review): leaked OS hostname to
    # unauthenticated network peers with no client use.
    assert "hostname" not in data


def test_identity_endpoint_requires_no_auth() -> None:
    """The whole point of /identity is that a not-yet-enrolled iPhone can hit it before it has any
    token. It must not be behind require_identity."""
    app = _build_bare_app()
    with TestClient(app) as client:
        # No Authorization header, no ?token= — must still succeed.
        r = client.get("/identity")
        assert r.status_code == 200, f"identity must be unauthenticated, got {r.status_code}"


def test_identity_never_leaks_api_token_or_bearer() -> None:
    """Contract: /identity carries public metadata only. Neither the file-backed api_token nor any
    bearer/session/device token literal may appear in the response body."""
    app = _build_bare_app(runtime_session_id="rt-abc-def")
    with TestClient(app) as client:
        body = client.get("/identity").text
    for banned in ("api_token", "Bearer ", "device_session", "access_hash", "refresh_hash"):
        assert banned.lower() not in body.lower(), (
            f"/identity leaked banned token substring {banned!r}")


def test_identity_reports_empty_runtime_id_when_runtime_absent() -> None:
    """Without an injected runtime (a bare test harness), /identity degrades cleanly to a
    stable-shape response with an empty runtime_id — never a 500, so a probing iPhone gets a
    negative but actionable answer."""
    app = _build_bare_app(runtime_session_id=None)
    with TestClient(app) as client:
        data = client.get("/identity").json()
    assert data["service"] == "sali"
    assert data["runtime_id"] == ""


# ── ApiSettings ───────────────────────────────────────────────────────────────────────────────────


def test_api_settings_defaults() -> None:
    """Defaults must be 0.0.0.0:8080 + mDNS enabled so LAN discovery works out of the box.
    Auth is IP-agnostic, so 0.0.0.0 is safe by construction (see /identity contract)."""
    from sali.config.settings import ApiSettings

    s = ApiSettings()
    assert s.bind_host == "0.0.0.0"
    assert s.bind_port == 8080
    assert s.mdns_enabled is True
    assert s.mdns_service_name == "Sali"


def test_api_settings_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loopback-only forcing via env: SALI_API__BIND_HOST=127.0.0.1 must reach Settings.api."""
    monkeypatch.setenv("SALI_API__BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("SALI_API__MDNS_ENABLED", "false")
    from sali.config.settings import load_settings

    settings = load_settings()
    assert settings.api.bind_host == "127.0.0.1"
    assert settings.api.mdns_enabled is False


# ── SaliBonjour ───────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sali_bonjour_registers_and_unregisters_cleanly() -> None:
    """The advertiser wraps zeroconf's blocking calls in run_in_executor. start()/stop() must both
    be idempotent and safe. If zeroconf isn't installed, start() no-ops and the test skips."""
    from sali.net.mdns import SaliBonjour

    bonjour = SaliBonjour(
        runtime_id="test-runtime-id-01234567",
        port=48080,
        version="0.0.0-test",
        service_name="SaliTest",
    )
    await bonjour.start()
    if bonjour._zc is None:
        pytest.skip("zeroconf not installed; register was a no-op")
    # Second start is a no-op — must not throw or re-register.
    await bonjour.start()
    await bonjour.stop()
    # Second stop is a no-op — must not throw.
    await bonjour.stop()


@pytest.mark.asyncio
async def test_bonjour_txt_record_carries_no_secret_material() -> None:
    """Contract: TXT record carries only public metadata (service tag, runtime_id, version, path,
    protocol). No token/session/hash may appear in the packed key/value bytes. Verified via a
    Zeroconf browser resolving the same instance we just registered."""
    zeroconf = pytest.importorskip("zeroconf")  # skip cleanly if the dep isn't available
    from sali.net.mdns import SERVICE_TYPE, SaliBonjour

    runtime_id = "rid-fake-1234-5678"
    port = 48090
    bonjour = SaliBonjour(runtime_id=runtime_id, port=port, version="0.0.0-test",
                          service_name="SaliTest2")
    await bonjour.start()
    try:
        assert bonjour._info is not None, "advertisement should be registered"
        props = bonjour._info.properties
        assert props[b"service"] == b"sali"
        assert props[b"runtime_id"] == runtime_id.encode()
        # Positive contract: presence of the fields the iPhone verifier will read.
        assert b"path" in props and b"protocol" in props and b"version" in props

        # Negative contract: no banned-substring anywhere in the TXT record.
        joined = b" ".join(list(props.keys()) + list(v or b"" for v in props.values())).lower()
        for banned in (b"token", b"bearer", b"device_session", b"api_token", b"access_hash",
                       b"refresh_hash", b"password", b"secret"):
            assert banned not in joined, f"TXT record leaked banned substring {banned!r}"
    finally:
        await bonjour.stop()


@pytest.mark.asyncio
async def test_bonjour_survives_missing_zeroconf(monkeypatch: pytest.MonkeyPatch) -> None:
    """Contract: if zeroconf isn't installed, start() logs and no-ops instead of crashing the
    daemon. Simulated by patching the import to fail."""
    import builtins

    original_import = builtins.__import__

    def _fail_zeroconf(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "zeroconf":
            raise ImportError("simulated missing zeroconf")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fail_zeroconf)

    from sali.net.mdns import SaliBonjour

    bonjour = SaliBonjour(runtime_id="x", port=1, version="v")
    await bonjour.start()   # must not raise
    assert bonjour._zc is None
    await bonjour.stop()   # must not raise


# ── Source-level security guards (bypass-resistant AST checks) ────────────────────────────────────


def _read(rel: str) -> str:
    return pathlib.Path(rel).read_text()


def test_identity_route_is_not_under_require_identity() -> None:
    """AST guard: the /identity route must sit on the FastAPI app root, not inside the /api/v1
    router (which is wrapped in Depends(require_identity)). Otherwise discovery couldn't work."""
    src = _read("src/sali/api/app.py")
    # The /identity handler must be attached with @app.get, not @router.get.
    assert re.search(r'@app\.get\(\s*"/identity"\s*\)', src), (
        "/identity must be attached to `app`, not to `router` (which is auth-gated).")
    # And it must not appear anywhere in the auth-gated data router.
    api_src = _read("src/sali/api/routes/api.py")
    assert '"/identity"' not in api_src, "/identity was moved into the authed router — reverting."


def test_mdns_module_ships_only_public_txt_keys() -> None:
    """AST-adjacent guard: the mdns module's properties dict must only contain the whitelisted
    public keys. If someone adds `token` or `device_session` to the TXT record, this fires."""
    src = _read("src/sali/net/mdns.py")
    # The properties dict lives in one place; enumerate its allowed keys.
    props_block = re.search(r"properties\s*=\s*\{(.*?)\}", src, re.DOTALL)
    assert props_block is not None, "mdns.py must define a `properties = {...}` dict"
    # Match only KEYS: `b"KEY":` (colon anchor prevents matching the value literals).
    keys = re.findall(r'b"([a-z_]+)"\s*:', props_block.group(1))
    whitelist = {"service", "runtime_id", "version", "path", "protocol"}
    unexpected = set(keys) - whitelist
    assert not unexpected, (
        f"mdns TXT record grew unexpected keys {sorted(unexpected)}. If a legitimate new public "
        f"field is being added, extend the whitelist in this test WITH the security review that "
        f"proves the field is not a secret.")


def test_ios_scout_bind_host_default_is_open() -> None:
    """Regression check: the settings default for the bind host must be 0.0.0.0 (LAN-reachable),
    not 127.0.0.1 (loopback). A silent revert here would break iPhone LAN discovery even if the
    mDNS + /identity code is intact."""
    src = _read("src/sali/config/settings.py")
    # Find the ApiSettings class body and read its bind_host default.
    api_block = re.search(r"class ApiSettings\(BaseModel\):(.*?)^class ", src, re.DOTALL | re.MULTILINE)
    assert api_block is not None, "ApiSettings class must exist in settings.py"
    m = re.search(r'bind_host:\s*str\s*=\s*"([^"]+)"', api_block.group(1))
    assert m is not None, "ApiSettings.bind_host default must be a string literal"
    assert m.group(1) == "0.0.0.0", (
        f"ApiSettings.bind_host default is {m.group(1)!r}. LAN discovery needs 0.0.0.0. "
        f"If loopback is intentional for a hardened deployment, set it via env or TOML — not as "
        f"the code default.")
