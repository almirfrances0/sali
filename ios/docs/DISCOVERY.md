# Sali LAN Discovery Protocol

The iPhone finds Sali on the same Wi-Fi without the user typing an IP. If Sali is not on the same
LAN — or Local Network permission is denied — the app falls back to `https://sali.salieno.com`
(Cloudflare Tunnel) automatically. Same Sali, same runtime, same memory, same tasks — the
transport is the only thing that changes.

> One Sali, many transports. The iPhone is a window; the mind lives on Kali.

---

## 1. Architecture at a glance

```
                             ┌───────────────────────┐
                             │  Sali daemon (Kali)   │
                             │  0.0.0.0:8080         │
                             │  runtime_id=01a06d0f… │
                             └──────────┬────────────┘
                                        │ advertises via mDNS
                            _sali._tcp.local. + TXT record
                                        │
     ┌──────────────────────────────────┴────────────────────────────────┐
     │                                                                    │
Home Wi-Fi                                                          Public Internet
     │                                                                    │
NWBrowser sees                                                    Cloudflare Tunnel
Sali._sali._tcp.local.                                            → sali.salieno.com
     │                                                                    │
     ↓                                                                    ↓
   iPhone ── verifies /identity ── connects locally  ←── or falls back ──→ iPhone
                (ws://<lan-ip>:8080/ws)                        (wss://sali.salieno.com/ws)
```

Both paths reach the **same** FastAPI app, the same WebSocket handler, the same event log. The
iPhone's WS `after_seq` watermark is valid on both sides because it's the same event table.

---

## 2. Service advertisement (backend)

The daemon runs one mDNS advertiser via [zeroconf](https://pypi.org/project/zeroconf/) in
`src/sali/net/mdns.py`. Started from `create_app()`'s lifespan when the API window comes up,
unregistered when it goes down. Not a separate daemon — no second Sali, no extra process.

**Service type**: `_sali._tcp.local.`
**Instance name**: `Sali._sali._tcp.local.` (default; configurable via `settings.api.mdns_service_name`)
**Port**: whatever the daemon bound to (default 8080; configurable via `settings.api.bind_port`)
**Addresses**: every non-loopback IPv4 the host has (Wi-Fi + wired interfaces both, if present)

### TXT record

Public metadata only. Never carries a token or session or credential. Verified by a source-level
test that whitelists exactly these keys (`test_mdns_module_ships_only_public_txt_keys`).

| Key          | Value                                       | Purpose |
|--------------|---------------------------------------------|---------|
| `service`    | `sali`                                      | Positive service tag — clients gate on this before trusting the peer |
| `runtime_id` | UUID (e.g. `01a06d0f-72dd-7501-…`)          | Stable across daemon restarts; from `persistent_session_id()` |
| `version`    | Package version (e.g. `0.0.0`)              | Informational |
| `path`       | `/identity`                                 | Where the unauth verification endpoint lives |
| `protocol`   | `1`                                         | Discovery-contract version; bump on breaking change |

### Configuration

```toml
# ~/.config/sali/sali.toml (all optional; defaults are LAN-reachable + mDNS on)

[api]
bind_host = "0.0.0.0"           # 127.0.0.1 forces loopback-only (mDNS still advertises but nothing reachable)
bind_port = 8080
mdns_enabled = true             # false → no Bonjour advertisement, iPhone must be told the IP or use remote
mdns_service_name = "Sali"      # rename for multiple-Sali test rigs on one LAN
```

Env-var equivalent: `SALI_API__BIND_HOST=127.0.0.1`, `SALI_API__MDNS_ENABLED=false`, etc.

---

## 3. Verification: `GET /identity`

An **unauthenticated** endpoint. Its whole purpose is to answer "are you a Sali?" BEFORE the
iPhone presents a bearer token to what might be a wrong or hostile responder.

**Method**: `GET`
**Auth**: none (there is no `Depends(require_identity)` on this route; it sits on the FastAPI
app root next to `/healthz`)
**Response**: `application/json`
```json
{
  "service": "sali",
  "runtime_id": "01a06d0f-72dd-7501-95ab-38223692bd4d",
  "version": "0.0.0",
  "hostname": "kali",
  "protocol": "1"
}
```

**Security contract** (verified in `tests/test_lan_discovery.py`):
* Response NEVER contains `api_token`, `Bearer `, `device_session`, `access_hash`, `refresh_hash`
  or any other token substring.
* Response degrades cleanly (`runtime_id=""`) if a Sali runtime hasn't been injected — never 500.
* Route MUST live on the app root, NOT inside the auth-gated `/api/v1` router.

---

## 4. Connection selection (iOS)

`ConnectionManager` at `SaliIOS/Core/Networking/ConnectionManager.swift` is the state machine.
It owns the decision; every other layer flows through the abstractions it depends on.

### States

| State                | Meaning |
|----------------------|---------|
| `.discovering`       | mDNS browser running; no transport decided yet |
| `.localConnecting`   | Verifying a discovered candidate via `/identity` |
| `.localConnected`    | Connected via LAN — pill shows `🟢 Sali · Local` |
| `.remoteConnecting`  | Verifying / connecting to sali.salieno.com |
| `.remoteConnected`   | Connected via Cloudflare — pill shows `🟢 Sali · Remote` |
| `.offline`           | No usable path (both Wi-Fi + cellular off, or auth lost) |
| `.reconnecting`      | Previous transport dropped; WS layer is backing off |

### Selection algorithm

1. On app foreground / enrollment / scenePhase active → `start()`:
   * Begin `NWBrowser(for: .bonjourWithTXTRecord(type: "_sali._tcp", domain: nil))`.
   * Begin `NWPathMonitor` (Wi-Fi / cellular / wired reachability).
   * Subscribe to `WebSocketClient.$state` (a socket drop is a signal we may need to re-choose).
2. On any discovery / path change, debounce 100 ms then `reconcile()`:
   * If path not satisfied → `.offline`.
   * If Wi-Fi/wired + a local candidate exists → verify via `/identity` (1.5 s timeout).
     * If the TXT record advertised a `runtime_id` AND `/identity` returned a different one,
       REFUSE the swap (poisoned mDNS / stale TXT — see security section below). Fall back to
       remote.
     * Otherwise `applyTransport(...)` on the app; wait ≤6 s for WS `.connected`.
   * Else → `switchToRemote()` (probes `https://sali.salieno.com/identity`, swaps if not
     already on remote).
3. If a live WS drops:
   * `.reconnecting(previousMode)` → the WS layer's own back-off applies (1s / 2s / 4s / … cap
     20s; 60s floor for auth refusals).
   * If the socket transitions to `.offline` (WS layer gave up), reconcile again — this is
     what tries LAN when Remote fails, or Remote when LAN fails.
4. Anti-flap: never swap transports faster than every **8 seconds** once we've swapped.

### Preferring LAN

* Never rediscovers if a candidate matching the last-known `runtime_id` is present.
* If two Salis are on the same LAN (rare — Sali is single-instance per machine, but a dev rig
  might), the candidate list is sorted by Bonjour instance name for a deterministic pick.

### Force-Remote

The diagnostics sheet has a "Force Remote" button. Calls `manager.forceRemote()` → immediate
`switchToRemote()`. The next reconcile will re-try LAN unless the user opts out per-session
(not currently persisted; a future addition if the flapping problem shows up in practice).

---

## 5. Chat MUST NOT break during a transport swap

**Design invariant**: a LAN↔Remote hop for the SAME Sali runtime does NOT reset the WebSocket
sequence watermark. Same event table, same after_seq, replay recovers anything missed during
the swap.

This is enforced by two separate methods on AppState:

| Path                                | Method                                        | Resets sequence? |
|-------------------------------------|-----------------------------------------------|------------------|
| User types a new URL in Settings    | `AppState.applyConfiguration(_:)`             | Yes — different backend → different event table |
| Auto LAN↔Remote swap                | `AppState.applyTransport(_:runtimeId:)`       | **No** — same Sali, same event table |

`ConnectionManager` only calls `applyTransport`. Guarded at source level by
`test_swap_path_preserves_sequence_not_resets` and by AST inspection in the review.

**How replay works after a swap**:
1. `applyTransport` calls `ws.disconnect()` + `ws.connect()`.
2. New socket opens, `authenticate()` runs with the same bearer token (which is device-scoped,
   not transport-scoped — same token works on either).
3. Client sends `{"type":"subscribe","after_seq": <lastSequence>}` with the WATERMARK IT ALREADY
   HAS (never reset).
4. Server calls `manager.replay_since(client_id, after_seq, pool)` — up to 500 events.
5. Any events published during the swap window are replayed.

**Queued outbound messages**: `ChatViewModel.send` is REST (not WS). The composer submit hits
`POST /api/v1/conversation/message` on the current APIClient's baseURL. If a message is in
flight during a swap, its response either lands before the swap (fine — reply streams over WS
on the new connection) or fails with a URL cancellation (the composer surfaces retry as a
first-class UX affordance — inherited from pre-existing chat resilience).

---

## 6. Security

Opening LAN access does NOT weaken authentication. The auth model is bearer-token, checked in
FastAPI dependencies — completely IP-agnostic. Every existing gate applies:

| Layer               | Guarded by                                                    |
|---------------------|---------------------------------------------------------------|
| `/api/v1/*`         | `Depends(require_identity)` at the router level (401 without) |
| Mutating `/api/v1/*`| `Depends(require_controller)` (403 for observers)             |
| Enrollment control  | `Depends(require_owner)` (host token or explicit owner device) |
| WebSocket handshake | `authenticate(pool, token)` — closes 4001 without a valid token |

### What discovery adds

* An unauth `/identity` endpoint returning **only** public metadata (service tag, runtime_id,
  version, hostname, protocol). Guarded by four source-level tests to enforce the "no secrets"
  invariant across future edits.
* mDNS advertisement carrying the same public metadata in a TXT record. Guarded by an
  AST-checked whitelist.
* Bind default flipped from `127.0.0.1` to `0.0.0.0` so LAN hosts can reach the API. Auth
  pipeline runs identically on LAN and tunnel packets.

### Attack surfaces + mitigations

**Rogue Bonjour service (F1)**. A hostile host on the same Wi-Fi advertises `_sali._tcp.local.`
with a plausible name and serves a matching `/identity`. Without a trust anchor, the iPhone
would swap to the rogue and open a WS with its real bearer token — the rogue harvests it.

Primary mitigation: **enrolled-runtime-id trust anchor**.

* On the first successful `/identity` probe after enrollment, `TokenStore.enrolledRuntimeId` is
  stamped with that Sali's `runtime_id`. This is persisted in UserDefaults and cleared only on
  `signOut()`.
* Every subsequent LAN discovery goes through `ConnectionManager.tryLocal` which **REFUSES** any
  candidate whose `/identity` reports a different runtime_id — before offering the bearer token
  and before swapping the WebSocket. `switchToRemote` performs the same check against
  `sali.salieno.com`, defeating a misconfigured tunnel that would route the app to a different
  Sali.
* `pickBestCandidate` prefers a candidate matching the enrolled runtime_id; if an anchor exists
  but NO candidate matches, the manager returns nil and reconcile falls through to remote — it
  never picks a wrong Sali just because that's the only Bonjour service visible.

Secondary mitigations (defense in depth):

* **TXT-runtime_id match**: if the Bonjour TXT record advertises a `runtime_id` that doesn't
  match what `/identity` returns, refuse — catches the accidental-lookalike case.
* **Bearer token gate at WS**: even without the anchor, the WS handshake calls `authenticate()`
  — a rogue would still need a valid `device_session` for THAT bearer to actually get further
  than the socket accept.
* **No credentials during probe**: `IdentityProbe` uses `URLSessionConfiguration.ephemeral` and
  explicitly clears the `Authorization` header on the probe request.

**LAN transport is cleartext HTTP/WS (F2 — KNOWN LIMITATION)**. `NSAllowsLocalNetworking`
permits `http://` on link-local / RFC 1918 destinations. On a shared/hostile Wi-Fi (open
network, hotel, café, conference) a passive sniffer on the same L2 segment can capture the
`Authorization: Bearer …` header on the WS upgrade and every chat frame. Auth is not
transport-based; it's bearer-token — a captured bearer works against the real Sali until it
expires.

Mitigations available today:
* **Use LAN only on TRUSTED Wi-Fi** (home). On shared/hostile Wi-Fi, the ConnectionManager can
  be forced to Remote via the diagnostics sheet's "Force Remote" button, which is persisted
  across reconciles and Bonjour pings. The user's choice survives until they explicitly tap
  "Auto-detect".
* The enrolled-runtime-id anchor prevents the token from being sent to the WRONG Sali; it does
  NOT prevent a same-Wi-Fi observer from reading it in transit to the right Sali.

Future work (not in this pass): serve the LAN endpoint over TLS with a per-runtime cert whose
fingerprint is pinned at enrollment. Deferred because it requires a cert-provisioning step and
Info.plist changes; the trust anchor + Force-Remote combination is judged sufficient for the
home-Wi-Fi use case this feature targets.

**LAN bind exposes API on every host interface (F3 — MITIGATED VIA AUTH)**. The daemon binds
`0.0.0.0:8080` by default so LAN discovery works out of the box. This means Docker containers
on `docker0`, WireGuard peers on `wg0`, and any host on any Wi-Fi the machine joins can reach
`/api/v1/*`. Every reachable endpoint still requires a valid bearer:

* `/api/v1/*` — router-level `Depends(require_identity)` (401 without) plus
  `require_controller` on mutating routes.
* `/api/v1/enroll` — needs a one-time pairing code minted by the owner (10-min TTL,
  cryptographic-random). Brute-forceable in principle; infeasible in practice.
* `/identity` and `/healthz` — public metadata / liveness only; no secrets.

For a hardened deployment, set `SALI_API__BIND_HOST=127.0.0.1` (env) or
`[api] bind_host = "127.0.0.1"` in `~/.config/sali/sali.toml`. LAN discovery will still
ADVERTISE (mDNS won't hurt anyone) but no LAN client will be able to reach the API — the app
falls back to Remote automatically.

**`/identity` no longer discloses OS hostname (F4 — FIXED)**. The initial implementation
returned `socket.gethostname()`; the diagnostics sheet doesn't need it and the disclosure was a
targeting aid on shared Wi-Fi. Removed in the turn-2 fold-in.

**LAN-scoped ATS exemption**. `NSAllowsLocalNetworking = YES` in the Info.plist permits
cleartext HTTP **only** to link-local and RFC 1918 addresses (10/8, 172.16/12, 192.168/16) and
`.local` names. Public Internet endpoints still require HTTPS. The remote transport goes to
`https://sali.salieno.com`, which is TLS end-to-end via Cloudflare.

**No secret in TXT record**. Verified by `test_mdns_module_ships_only_public_txt_keys` which
grep-whitelists the exact keys the module ships. Any new key requires updating the whitelist
with an explicit security review.

---

## 7. Diagnostics

The 🟢 / 🟡 / ⚫ pill in the chat header opens a diagnostics sheet showing:

* Current mode (Local / Remote), endpoint, probe latency
* Runtime ID (from the last successful `/identity` probe)
* Version + hostname
* Last error string (if any)
* Last-known-good Local + Remote configurations
* Path reachability (Wi-Fi / cellular / wired / none, expensive/metered)
* Bonjour: browsing? services visible? which ones?
* Force-Remote button

Nothing here is sent home; it's a read-only view of what the ConnectionManager already knows.

---

## 8. What was NOT done (deliberately)

* **A separate discovery daemon.** Advertisement lives in the API's lifespan — one process, one
  Sali, one mind. Turning the daemon off makes the service disappear naturally.
* **A LAN-only unauthenticated admin endpoint.** Auth model stays uniform. There is no "trusted
  IP" bypass anywhere in the code (verified by grep on `request.client`, `127.0.0.1`,
  `trusted_hosts`).
* **A separate iOS discovery target/framework.** All of it is one Swift file (~350 LoC) plus
  three small primitives, wired into AppState through an existing seam
  (`applyConfiguration`/`applyTransport`).
* **A CoreData/second cache for LAN candidates.** `lastKnownGoodLocal` is in-memory; discovery
  is fast enough that re-browsing on foreground is a non-issue.
* **A SwiftUI dependency-injection framework.** `ConnectionApplying` is a hand-written protocol
  with a mock in the test file — this is one seam, not a framework.

---

## 9. Test matrix

Backend (`tests/test_lan_discovery.py`, 12 passing):

* `/identity` returns correct shape, no auth needed
* `/identity` never leaks token/session/hash literals
* `/identity` degrades cleanly without an injected runtime
* `ApiSettings` defaults are `0.0.0.0` + `mdns_enabled=True`
* `ApiSettings` env override works (`SALI_API__BIND_HOST=127.0.0.1`, `SALI_API__MDNS_ENABLED=false`)
* `SaliBonjour.start()/stop()` idempotent
* TXT record whitelist enforced (no `token/bearer/device_session/api_token/access_hash/refresh_hash`)
* Missing zeroconf → advertiser no-ops rather than crashing
* AST guard: `/identity` route is on `app`, not the `require_identity`-gated router
* AST guard: `mdns.py` `properties = {...}` dict only contains whitelisted public keys
* AST guard: `ApiSettings.bind_host` default is `"0.0.0.0"` (not `127.0.0.1`)

iOS (contracts encoded in the source and adversarially reviewed):

* `applyTransport` MUST NOT call `resetSequence` (LAN↔Remote preserves chat)
* `LinkState.glyphEmoji` and `.shortLabel` render as the documented pill values
* `IdentityProbe` uses `URLSessionConfiguration.ephemeral` + explicitly strips `Authorization`
* Bonjour permission denial is handled; app falls back to Remote gracefully
* Anti-flap: 8 s minimum between transport swaps
* TXT `runtime_id` vs `/identity` `runtime_id` mismatch refuses the swap

Manual (for Almir to run against his iPhone):

* Same Wi-Fi as PC → 🟢 Sali · Local within ~2 s
* Turn off PC → drops to 🟡 → 🟢 Sali · Remote (or ⚫ if Cloudflare down)
* Turn PC back on same Wi-Fi → returns to 🟢 Sali · Local
* Toggle Wi-Fi off → 🟢 Sali · Remote (over cellular)
* Kill Internet, keep Wi-Fi → stays on Local
* Force Remote → 🟢 Sali · Remote until next reconcile
* Deny Local Network permission → app stays on Remote (never crashes)
* Send message during a transport swap → reply arrives on the new transport via replay

---

## 10. See also

* Backend implementation: `src/sali/net/mdns.py`, `src/sali/api/app.py`, `src/sali/config/settings.py`
* iOS implementation: `SaliIOS/Core/Discovery/BonjourDiscovery.swift`,
  `SaliIOS/Core/Networking/{IdentityProbe,NetworkPathObserver,ConnectionManager}.swift`,
  `SaliIOS/Features/Chat/ConnectionStatePill.swift`
* Tests: `tests/test_lan_discovery.py`
* Cloudflare tunnel setup: [`CLOUDFLARE.md`](CLOUDFLARE.md)
* API reference: [`API_REFERENCE.md`](API_REFERENCE.md#identity)
