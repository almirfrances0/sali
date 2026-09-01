# Sali API Reference — iPhone Control Center

The complete backend contract the Sali iOS app is built against (Prompt 13). Written so a developer on
macOS can continue the app in Xcode **without rediscovering backend behavior**. Everything here is
implemented and tested in `src/sali/api/` (see `tests/test_api_*.py`).

> **One Sali, many interfaces.** The terminal and the iPhone share the same Sali instance, session, and
> durable state. The app is a *secure window* into Sali — never a second task system, never a bypass around
> Sali's safety, workspace, or task authority.

---

## 1. Base URL & configuration

| Environment | Base URL | Notes |
|-------------|----------|-------|
| Development | `http://<kali-lan-ip>:8080` | `sali serve` on the LAN; enroll over HTTP on trusted Wi-Fi |
| Production  | `https://sali.salieno.com` | Cloudflare Tunnel → origin (§ Cloudflare) |

There is no hardcoded production URL in feature code — the app resolves the base URL from a selected
`APIEnvironment` (see `Core/Networking/APIConfiguration.swift`). No secrets live in the repo.

REST is versioned under `/api/v1`. The WebSocket is `/ws`. A public liveness probe is `GET /healthz`
(no auth, no data) for uptime checks and tunnel health.

---

## 2. Authentication model

Two credential kinds resolve to one identity server-side:

- **Device** — a short-lived **access token** issued to an enrolled iPhone. This is what the app uses for
  every REST/WS request. Revocable, expiring, and carries a **role** (`owner` / `controller` / `observer`).
- **Host** — the local API token on the Kali box (file `~/.config/sali/api_token`, mode 0600). Never leaves
  the machine; used only to bootstrap enrollment and as local break-glass. **The app never receives it.**

Send the access token as `Authorization: Bearer <access_token>` on every request. The whole `/api/v1` data
surface requires a valid identity (401 otherwise). Mutating routes additionally require `controller` or
`owner` (observers are read-only → 403).

**Never** rely on obscurity or a `User-Agent`. A public HTTPS URL is not a security boundary — the token is.

---

## 3. Device enrollment (one-time)

A fresh install has **no authority**. It must be paired once:

```
host: sali enroll-code            →  prints  XXXXX-XXXXX  (single-use, expires in ~10 min)
        │
iPhone: POST /api/v1/enroll {code, name, model}
        │
server: creates api_device + device_session, marks the code used
        │
iPhone: stores access + refresh tokens in the iOS Keychain → device is authorized
```

The pairing code is minted on the host (CLI `sali enroll-code`, or an owner device via
`POST /api/v1/enroll/code`). It is **single-use** and **expires**; it never becomes a permanent credential.
Only a SHA-256 hash of the code is stored server-side.

### `POST /api/v1/enroll/code`  *(owner only)*
Mint a pairing code from an already-trusted owner (host token or owner device).
```jsonc
// request
{ "role": "owner", "label": "Almir's iPhone 15 Pro" }   // role ∈ owner|controller|observer
// response
{ "code": "K7M9P-R3TQ2", "role": "owner", "expires_at": "2026-08-31T18:40:00Z" }
```

### `POST /api/v1/enroll`  *(no auth — the code is the one-time authority)*
```jsonc
// request
{ "code": "K7M9P-R3TQ2", "name": "Almir's iPhone", "model": "iPhone16,1", "platform": "ios" }
// response 200 — the plaintext tokens are returned exactly once
{
  "device_id": "…uuid…",
  "role": "owner",
  "access_token": "…",           // short-lived (~15 min)
  "refresh_token": "…",          // longer-lived (~30 days), rotates on use
  "access_expires_at": "…",
  "refresh_expires_at": "…"
}
// 401 — unknown / used / expired code (never says which)
```

Store **both** tokens in the Keychain immediately. The response is the only time the plaintext exists.

---

## 4. Token lifecycle

- **Access token** (~15 min): sent on every request. On `401`, refresh and retry once.
- **Refresh token** (~30 days): exchanged for a new pair. Refresh **rotates** — the old refresh token is
  revoked when a new one is issued, so a replayed refresh token is rejected (theft-resistant).

### `POST /api/v1/auth/refresh`  *(no auth — carries the refresh token)*
```jsonc
{ "refresh_token": "…" }        // → same shape as /enroll response (new access + new refresh)
// 401 — invalid / expired / already-rotated / revoked
```

Client rule: a single in-flight refresh, queue concurrent 401s behind it, then replay. If refresh returns
401, the device is no longer trusted → drop to the onboarding screen.

---

## 5. Device management  *(owner only)*

| Method & path | Purpose |
|---|---|
| `GET /api/v1/devices` | List enrolled devices: role, status, last-seen, push registration. |
| `POST /api/v1/devices/{id}/revoke` | Revoke a device — kills **every** session bound to it immediately. |
| `POST /api/v1/devices/{id}/push-token` | Register/refresh an APNs token. A device may set its own; owner may set any. |

`DeviceResponse`:
```jsonc
{ "id":"…","name":"Almir's iPhone","platform":"ios","model":"iPhone16,1",
  "role":"owner","status":"active","has_push":true,"push_environment":"production",
  "created_at":"…","last_seen_at":"…","revoked_at":null }
```

Push-token registration:
```jsonc
{ "token": "<apns device token hex>", "environment": "production" }   // or "sandbox"
```

---

## 6. WebSocket — real-time stream & recovery

`ws(s)://<base>/ws` — authenticate with the access token via the `Authorization: Bearer` header (or, for
clients that can't set WS headers, `?token=<access>`). Unauthenticated connections are closed with code
**4001** before accept. Connection cap: 5 (close code 4029 if exceeded).

### Lifecycle
```
connect (auth) → {"type":"connected","client_id":…}
client → {"type":"subscribe","after_seq": <last seq seen, or 0>}
server → replays every durable event with seq > after_seq  (bounded to 500)
server → {"type":"subscribed","replayed": N}
… live events stream …
client → {"type":"ping"}   server → {"type":"pong"}
```

### Event frame (live and replayed are identical)
```jsonc
{
  "type": "event",
  "event_type": "task.progress",
  "event_id": "…",
  "sequence": 1423,               // monotonic; track the max you've seen
  "timestamp": "…ISO8601…",
  "task_id": "…", "run_id": "…", "session_id": "…",
  "origin": "task",               // agent|tool|task|watchdog|runtime|system
  "data": { … },                  // event-specific, never chain-of-thought
  "replayed": true                // present only on recovery replays
}
```

### Recovery (§29) — the client's contract
1. Track `maxSeq` = the highest `sequence` seen from any event (live or replayed).
2. On (re)connect, send `{"type":"subscribe","after_seq": maxSeq}`.
3. The server replays the gap from the authoritative `event` table, then goes live. No missed events, no
   duplicates (the client can also dedupe by `sequence`).
4. If offline longer than the 500-event replay bound, backfill with `GET /api/v1/events?after_seq=maxSeq`
   (paginate) before resubscribing.

Reconnect **always** re-authenticates — there is no unauthenticated path, and a rotated/expired token must
be refreshed first.

### Event types the app renders — the REAL vocabulary the backend emits

The turn stream is published as `agent.{kind}` (coordinator) and task lifecycle as `task.*` (task store).
An earlier draft of this doc listed idealized `message.*`/`task.completed` names that are **not** emitted —
build to the names below (the iOS `EventType` maps exactly these):

- **Chat turn stream** (`origin:"agent"`): `agent.run` (turn started) · `agent.thinking` · `agent.status` ·
  `agent.retrieval` · `agent.token` (a streamed chunk — text in `data.text`) · `agent.tool` (tool activity)
  · `agent.final` (the settled assistant reply, text in `data.text`).
- **Tools**: `tool.completed` · `task.tool.started` · `task.tool.completed`.
- **Task lifecycle** (`origin:"task"`): `task.created` · `task.activated` · `task.progress` ·
  `task.step.completed` · `task.step.failed` · `task.waiting_for_user` · `task.finished` (carries
  `data.status = done|failed|abandoned` — there is **no** `task.completed`) · `task.suspended` ·
  `task.resumed` · `task.superseded` · `task.cancelled`.
- **Proactive & signals**: `agent.message` (`data.channel="agent_message"` — Sali reaching out) ·
  `intent.revoked` · `resource.incident_recorded` · `error`.

Chat rendering: treat `agent.token` as the delta (append `data.text` to the streaming bubble) and
`agent.final` as completion. `research.started`/`research.completed` may appear when research runs.

**Never** display chain-of-thought. High-level activity labels only ("Thinking", "Checking files",
"Running verification", "Saving progress").

---

## 7. Chat

- `GET /api/v1/conversation?limit=50` → `ConversationState` (shared across interfaces).
- `POST /api/v1/conversation/message` *(controller)* `{ "content": "…" }` → `{ "run_id":…, "status":"accepted" }`.
  The turn is serialized through the single runtime; the **response streams over the WebSocket** as
  `agent.run` → `agent.token*` (chunks; text in `data.text`) → `agent.final`. Do not poll for the answer.
  (The returned `run_id` is an acceptance token; correlate streamed events by the run_id carried on the
  `agent.*` frames.)
- `POST /api/v1/conversation/cancel` *(controller)* → interrupt the running turn.

**Agent-initiated messages** (§10) arrive as `agent.message` events with `data.channel == "agent_message"`
and `origin == "agent"` — Sali reaching out (a discovery, a completion, a request for clarification). Render
them inline but visually distinct from the user's own turns; they are **not** fake user messages.

Markdown: assistant content is Markdown. Render headings, bold/italic, lists, blockquotes, inline code, code
blocks (language-aware, copy button, horizontal scroll), tables, links, and rules.

---

## 8. Asynchronous conversation & pending questions

Waiting is a property of a specific **activity/question**, never a global freeze (§12).

- `GET /api/v1/pending-questions?person=Almir` → questions Sali is waiting on (dependency-aware).
- `POST /api/v1/pending-questions/answer` *(controller)*
  `{ "person":"Almir", "answer":"go with option B", "thread_id":"…"? }` → routes a **late** free-text answer
  to the right question. Ambiguous → `{ "status":"ambiguous", "candidates":[…] }` (show a chooser).
- `GET /api/v1/conversations?person=…` → open conversation threads (several can be in flight at once).

---

## 9. Tasks

| Method & path | Purpose |
|---|---|
| `GET /api/v1/tasks` | Open/active tasks + `active_task_id`. |
| `GET /api/v1/tasks/{id}` | Full `TaskResponse` (objective, status, steps, workspace, retries…). |
| `GET /api/v1/tasks/{id}/workspace` | Authoritative workspace roots + mode. |
| `GET /api/v1/tasks/{id}/skills` | Selected skill snapshots. |
| `GET /api/v1/tasks/{id}/research` | Research findings + learning candidates. |
| `GET /api/v1/tasks/{id}/decisions` · `/phases` · `/reviews` · `/question` · `/context` | Ledger / phases / reviewer verdict / pending clarification / context budget. |
| `POST /api/v1/tasks/{id}/pause` *(controller)* | Suspend safely → `paused` (resumable as the same task). |
| `POST /api/v1/tasks/{id}/resume` *(controller)* | Resume → `running`. |
| `POST /api/v1/tasks/{id}/abandon` *(controller)* | **Intent revocation** — tombstone + cancel dependents + remove the live row. Will **not** auto-resume. |
| `POST /api/v1/tasks/{id}/revive` *(controller)* | Create a NEW task from a tombstoned objective; records the supersession. |

Task statuses: `open` · `running` · `paused` · `waiting_for_user` · `blocked` · `done` · `failed` ·
`abandoned` · `cancelled` · `superseded`.

**Abandoned tasks are never presented as active** (§15). They disappear from `/tasks` and 404 on GET; they
appear in `GET /api/v1/intent/revoked` as historical, revivable items.

`TaskResponse` (abridged):
```jsonc
{ "id":"…","objective":"…","status":"running","result":null,"is_primary":true,
  "workspace_root":"…","workspace_mode":"strict","retry_count":0,"max_retries":3,
  "superseded_by":null,"created_at":"…","updated_at":"…","interrupted_at":null,
  "steps":[{"seq":1,"description":"…","status":"done","verified":true,"attempts":1,
            "last_error":null,"failure_class":null,"checkpoint":{…}}] }
```

---

## 10. File transfer

Large transfers go over HTTP, **never** WS frames. The app references files by **id** (download) or a bare
**filename** (upload); the server maps to a real path and refuses anything outside the task workspace —
**the host path is never exposed** (§9/§33).

- `GET /api/v1/tasks/{id}/artifacts` → metadata:
  ```jsonc
  [{ "id":"…","filename":"report.pdf","artifact_type":"created","tool_name":"…",
     "size":10241,"available":true,"content_type":"application/pdf",
     "download_url":"/api/v1/tasks/{id}/artifacts/{artifactId}/download" }]
  ```
  (There is **no** `artifact_path` field — by design.)
- `GET …/artifacts/{artifactId}/download` → streams the file (`Content-Disposition` filename). `403` if the
  recorded path resolves outside the workspace; `410` if the file is gone.
- `POST /api/v1/tasks/{id}/files?filename=<name>` *(controller)* — raw body = file bytes; name sanitized to a
  single component; ≤ 50 MB; stored under `<workspace>/uploads/` and recorded as an artifact. (`X-Filename`
  header also accepted instead of the query param.)

---

## 11. Memory, experience, learning, capabilities

| Path | Purpose |
|---|---|
| `GET /api/v1/memory` | Counts by layer + recent experiences. |
| `GET /api/v1/experiences?objective=…` | Experiences shaping Sali (relevant, or recent). |
| `GET /api/v1/memory/conflicts` | Recorded contradictions (never silently overwritten). |
| `GET /api/v1/memory/{id}` | Provenance: source, task/run, evidence, confidence. |
| `GET /api/v1/learning` · `/learning/candidates` · `/learning/recent` · `/learning/contradictions` | Learning health / verified lessons / recent / conflicts. |
| `GET /api/v1/capabilities` · `/capabilities/overview` · `/capability-acquisitions` | What Sali can do, self-describing, and gaps being closed. |
| `GET /api/v1/behavior/profile` · `/behavior/proposals` | Derived behavior + pending proposals. |
| `POST /api/v1/behavior/proposals/{id}/approve` · `/reject` *(controller)* | The user is the authority on behavior changes. |

Every memory carries **provenance + confidence + freshness**. Surface those; never present weak inference as
fact. Destructive memory actions must be deliberate (confirm naturally, never one-tap).

---

## 12. Autonomous life

`GET /api/v1/current-life` (compact snapshot) · `/goals` · `/initiatives` · `/routines` · `/obligations` ·
`/commitments` · `/digital-objects` · `/digital-actions` · `/external-entities` · `/relationships` ·
`/timeline?limit=40` · `/side-effects` · `/cleanup` · `/consent` (+ `POST /consent/{id}/respond`).

Autonomy stays **visible and manageable**: every initiative shows why it started, its status, and how to stop
it. Digital identities/accounts are generic (`object_type`, provider, capabilities) — **no secrets ever
displayed**, and no provider is hardcoded.

---

## 13. System health

- `GET /api/v1/system` → one consolidated view for the System screen:
  ```jsonc
  {
    "model": "sali:latest",
    "resources": {
      "state": "safe",            // safe|elevated|high|critical|emergency
      "preservation": false,      // Prompt 12 preservation mode
      "gpu_util_pct": 41.0, "vram_used_pct": 63.2,
      "ram_used_pct": 38.0, "disk_used_pct": 55.0,
      "cpu_load": 1.2,            // load per core; "temperature_c": 61.0,
      "available": true,          // false if the host reading failed
      "raw": { "state":"…","reading":{…fractions…},"preserve":false }  // nested source, technical view
    },
    "health": { "all_ok":true,"datastore":true,"model":true,"embedder":true,"internet":true },
    "websocket_connections": 1
  }
  ```
  Any metric that can't be read is `null` (and `available:false`) — never a fabricated number.
- `GET /api/v1/resources` → the same flat `resources` object (Prompt 12 state ladder + preservation).
- `GET /api/v1/resource-incidents?kind=…` → durable host-endangering incidents (negative operational
  knowledge). These also stream as `resource.incident_recorded` events.

Connect metrics to **meaning**: "GPU memory elevated → Sali reduced context pressure → stable",
"Preservation mode active → heavy work paused".

---

## 14. Unified cognitive snapshot

`GET /api/v1/cognitive` (alias `/current-life`) → the single "what is Sali doing right now?" view the
Activity screen reconstructs from durable state: `task_id`, `objective`, `next_action`, `workspace`,
`phase`, `selected_skills`, `research_count`, capabilities, pending consent/question, resource state. Never
chain-of-thought.

---

## 15. Event recovery endpoint (HTTP)

`GET /api/v1/events?after_seq=<n>&limit=<≤500>` → durable events ordered by monotonic `seq`. Used to backfill
gaps larger than the WS replay bound before resubscribing.
```jsonc
[{ "seq":1423,"type":"task.finished","subject_type":"task","subject_id":"…",
   "payload":{ "status":"done", … },"timestamp":"…" }]
```

---

## 16. Errors

| Status | Meaning | App action |
|---|---|---|
| 400 | Bad request | Show the `detail` message. |
| 401 | Unauthenticated / expired access token | Refresh once, retry; on repeat → onboarding. |
| 403 | Authenticated but not authorized (observer, or non-owner) | Explain the role limit. |
| 404 | Not found (task/artifact/device/memory) | Refresh the list. |
| 409 | Conflict (e.g. task has no workspace) | Show `detail`. |
| 410 | Gone (artifact no longer on disk) | Mark unavailable. |
| 413 | Too large (>1 MB JSON, >50 MB upload) | Reject client-side too. |
| 429 | Too many connections (WS 4029) | Back off and retry. |
| 5xx / offline | Server/tunnel unavailable | Offline state; keep last-known, retry with backoff. |

Body shape: `{ "detail": "human-readable reason" }` (FastAPI default) or `{ "error": "…" }` (size middleware).

---

## 17. Security assumptions

- Cloudflare + Tunnel is **one** layer, not the only one. The API enforces auth/authorization itself
  (defense in depth). Never expose the origin port publicly; the tunnel is the only ingress.
- No DB admin, no shell, no `/run?command=…`. The app interacts only through structured operations. Sali's
  policy engine, task authority, and workspace boundary remain authoritative — the app cannot bypass them.
- Tokens live only in the iOS Keychain. Only hashes are stored server-side. Secrets are never logged, never
  returned after issuance, never shown in plaintext.
- Roles: `owner` (everything, incl. device management) ⊃ `controller` (send/control) ⊃ `observer`
  (read-only). The first device is the owner; the API is designed so future roles/devices are additive.

See `CLOUDFLARE.md` for the deployment topology and `XCODE_HANDOFF.md` for building on macOS.
