# Sali iOS — Grounding Document & Build Plan
### Premium · Minimal · Monochrome · Real-Time · Chat-Centric

**Architecture invariant (non-negotiable):** The app is *only an interface* to the single Sali runtime. It never spawns a second cognition, never fabricates endpoints or event names, and never calls model inference through any path but `POST /api/v1/conversation/message`. Every capability below is wired to a **verified** REST route (report A) or a **verified** WS `event_type` (report B). Anything not in A/B is called out explicitly as a backend gap with an architecture-consistent proposal — not invented and shipped.

**One-mind consequences the UI must respect:**
- There is exactly **one cognition slot** (`_cognition` lock + cross-process `ExecutionLease`). "Send" is fire-and-forget (`202 accepted`); the reply *always* arrives over WS, never in the HTTP response.
- `agent.token` / `agent.thinking` are **ephemeral** (`sequence: null`, never replayed). Durable turn state is `agent.run → agent.status → agent.tool → agent.final`.
- Order and dedupe on **`seq`** (REST) / **`sequence`** (WS), never on `ConversationMessage.id` (which is the session_id — a verified quirk).

---

## 1. Verified Capability Matrix

| Capability | Backend support (A/B — exact names) | Current app (C) | GAP |
|---|---|---|---|
| **Streamed chat** | `POST /api/v1/conversation/message` `{content}` → `202 {run_id,status:"accepted"}`; stream = WS `agent.run` → `agent.token`(ephemeral) → `agent.final`(durable) | `ChatViewModel.ingest()` state machine; `messageStarted/Delta/Completed`; idempotent via `processedEventIDs`; replayed events skipped | **None functionally.** Polish only: scroll stability + delta-append perf (§4 M2). |
| **Markdown / code** | payload is plain `content: str`; rendering is client-side | `MarkdownText.swift` (dep-free block parser), `CodeBlockView.swift` (copy + h-scroll, no highlight) | **None.** Optional: monochrome code theme in M1. |
| **Per-message send status** | Send returns `run_id`; progression inferable from `message.queued`/`message.dequeued` + `attention.turn_started` + `agent.run`/`agent.final` | **ABSENT.** `ChatMessage` has no status field; only global `isSending`/`sendError`; failure = global alert | **Add per-message status model** driven by the run/queue events. §4 M2. |
| **Message queue** | Durable inbox (`incoming_message`); events `message.queued` / `message.dequeued`, `attention.message_received` / `_classified`; `queue_for_later` → `{status:"queued"}`. **No REST queue-depth endpoint** (only leaks via `conversation/cancel.was_busy`) | **ABSENT.** Send is *blocked* while `isSending`; no outbox, no queued concept | **Build client-side queue + WS-inferred busy/queued badges.** Backend gap #3. §4 M4. |
| **In-chat task/activity disclosure** | `agent.status` (human phase), `agent.tool` (`phase`,`name`,`ok`,`summary`), `task.tool.started/completed`, `task.progress`, `task.step.completed/failed`, `task.waiting_for_user` | **PARTIAL.** Single `activity` label under streaming bubble (`humanSummary` only). Full disclosure only on Activity/Task tabs | **Add inline expandable activity trail** in the chat thread. §4 M2. |
| **File send** | `POST /api/v1/tasks/{task_id}/files` — **raw bytes**, `?filename=`/`X-Filename`, ≤50 MB, `application/octet-stream`, NOT multipart; **requires a task with bound `workspace_root`** (409 otherwise). No global upload. | `APIClient.upload(taskId:filename:data:)` **exists, zero callers**. Input bar is text-only | **Wire upload;** solve "which task_id?" (backend gap #1/#4). §4 M3. |
| **File receive** | Files Sali produces = **task artifacts**: `GET /tasks/{id}/artifacts` → `download_url`; `GET …/artifacts/{aid}/download` (FileResponse). Discovery via task/tool WS events; **no incoming-file push event** | `TaskDetailView` lists + downloads artifacts + ShareLink. Not in chat | **Surface artifacts inline in chat** on `task.tool.completed`/artifact events. §4 M3. Backend gap #2. |
| **Image send + vision** | **NO multimodal contract.** `SendMessageRequest` = `{content:str}` only; `handle_message` text-only. Backend "vision" = local host-screen `see_screen` tool. Image can only enter as a raw-bytes **task file**, then be *referenced in text* | **ABSENT.** No `PhotosUI`/`PHPicker`; message payload is `["content":text]` only | **Blocked on backend.** Interim: upload-as-task-file + textual reference. Backend gap #5 (real fix). §4 M5. |
| **Sali-initiated messages** | `agent.message` (origin `agent`, `data.channel=="agent_message"`, `importance`∈progress/update/milestone/question/warning/completion/failure). Plus `agent.reengagement`, `agent.follow_up_suppressed`. **Note: `send_agent_message` has no internal caller yet** — fired externally | **PRESENT.** `agent.message` → distinct `.agent` bubble "Sali • Proactive"; also in Notifications | **Harden + importance styling + reengagement handling.** §4 M6. |
| **Notifications / event-replay** | In-band WS `{"type":"notification",{title,body}}` and `task_update`. **APNs storage exists, NO sender.** Replay: `subscribe {after_seq:N}` → `event WHERE seq>N LIMIT 500`; REST backfill `GET /events?after_seq=&limit=` | `NotificationsView` reads `liveEvents` (in-memory only). WS `subscribeForRecovery()` sends `after_seq`. `PushManager` fully built but **never instantiated**; Settings toggle `.disabled(true)` | **Persist notifications; harden replay gap-recovery; APNs blocked on backend sender.** §4 M7. |
| **Connection / reconnect** | WS `/ws`, Bearer header or `?token=`; `connected`/`subscribed`/`pong`; `MAX_CONNECTIONS=5` (4029), `MAX_REPLAY=500`; reap every 60s (4001 on revoke/expiry) | **STRONG.** `WebSocketClient` backoff 1→20s, dual ping (native + `{"type":"ping"}` 12s), `ConnectionBadge` everywhere, `after_seq` recovery | **None.** Keep as-is; verify 4029/4001 close-code UX (§5 M2). |
| **Enrollment / auth** | `POST /enroll` (code), `POST /auth/refresh` (single-use rotation), `POST /enroll/code` (owner), `/devices*`. Access TTL **15 min**, refresh 30 d | **Complete.** `OnboardingView`, `AuthService`, single-flight 401-driven refresh, Keychain, `DevicesView` | **None.** Optional: proactive refresh using the already-defined-but-unused `TokenStore.accessExpiredOrExpiring`. |

---

## 2. Backend Gaps (architecture-consistent proposals)

Each respects one-mind/one-runtime and adds **no** parallel cognition. Ordered by product impact.

**Gap 1 — Multimodal message contract (the big one; blocks true image+vision).**
Today an image can only reach Sali as a raw task-file plus a text reference; there is no way to attach an image to a conversation turn for the model.
*Proposal:* extend the **existing** `SendMessageRequest` to `{content: str, attachments?: [{artifact_id | inline_ref}]}` and thread it into the single `runtime.handle_message(message, attachments=…)`. Reuse the existing raw-bytes upload to land the bytes first; the message just references them. No new cognition, no new inference path — same coordinator, same turn. Until this lands, the app uses the interim flow (M5).

**Gap 2 — "Which task?" for chat file send.**
`POST /tasks/{id}/files` needs a `task_id` with a bound `workspace_root` (409 otherwise), but a chat message has no inherent task.
*Proposal (client-side, no backend change needed to start):* target the **`active_task_id`** from `GET /tasks` / `GET /status`; if none, disable attach with a clear affordance ("attach available while a task is active"). *Preferred backend proposal:* a default/ambient conversation workspace so chat uploads always have a home — but do not block M3 on it; ship the active-task path first.

**Gap 3 — REST-exposed queue/busy indicator.**
`is_busy`/`queue_size` exist in the coordinator but leak only via `conversation/cancel.was_busy`.
*Proposal:* the app **infers** busy/queued state purely from the WS stream (`message.queued`→queued, `message.dequeued`/`attention.turn_started`→processing, `agent.run`→streaming, `execution.completed`→done). Backend nicety: add `queue_size`/`is_busy` to the existing `GET /api/v1/status` `SystemStatus` (read-only, no new route). Client ships on inference; the status field is a robustness upgrade.

**Gap 4 — Incoming-file push signal.**
No dedicated "Sali sent you a file" event; discovery is via task artifact lists.
*Proposal:* client keys off existing `task.tool.completed` / `agent.tool phase:"done"` for the task, then refreshes `GET /tasks/{id}/artifacts`. Backend nicety: include `artifact_id` in the `task.tool.completed` `data` so the client can deep-link the download without a full artifacts refetch.

**Gap 5 — Typing/presence (optional, cheap).**
WS receive loop accepts only `ping`/`subscribe`; report B explicitly recommends a transport-only `{"type":"typing"|"presence"}` branch that never touches the coordinator (zero inference, ephemeral). *Proposal:* defer; only add if the "living intelligence" feel needs a presence dot. Must stay inference-free.

**Gap 6 — APNs sender.**
Tokens are stored (`/devices/{id}/push-token`), `has_push` exposed, but there is **no HTTP/2 Apple sender anywhere**. Notifications ship in-band over WS as `{"type":"notification"}`.
*Proposal:* keep the app's `PushManager` ready; do not enable the Settings toggle until the backend sender exists. Until then, the WS `notification` frame is the delivery path (works only while connected — honest limitation).

**Gap 7 — Conversation back-pagination.**
`GET /conversation` takes `limit` only; no `before`/cursor.
*Proposal:* accept newest-N for history; use `GET /events?after_seq=` for *forward* recovery. If deep scrollback is needed later, propose adding `before_seq` to the existing route. Not a launch blocker.

---

## 3. Reuse List (keep & extend — do not rebuild)

**Core layer — production-quality, reuse wholesale:**
- `Core/Networking/APIClient.swift` — actor, single-flight 401 refresh, typed verbs. **Activate `upload(taskId:filename:data:)`** (already correct: raw octet-stream + `?filename=`, matches A §4). Extend for attachments when Gap 1 lands.
- `Core/Auth/AuthService.swift`, `TokenStore.swift` (Keychain, `WhenUnlockedThisDeviceOnly`), `Keychain.swift` — complete.
- `Core/WebSocket/WebSocketClient.swift` — multicast bug fixed, backoff, dual ping, `after_seq` recovery, `ConnectionState`. Keep; extend the receive path if presence (Gap 5) is ever added.
- `Core/Realtime/SaliEvent.swift` — `EventType.init(rawType:)` already maps the **real** backend names. Extend, don't replace (add `message.queued`/`message.dequeued`, `attention.*`, `agent.reengagement`, `execution.*`).
- `Core/Networking/APIError.swift`, `Core/Config/APIEnvironment.swift`, `Models/DomainModels.swift`.

**Chat — extend, not rebuild:**
- `Features/Chat/ChatViewModel.swift` — the `ingest()` state machine, idempotency set, replay-skip, timeout watcher are all correct. **Extend `ChatMessage` with a `status`**; delete dead `ingestNew(from:)`.
- `Features/Chat/ChatView.swift`, `MarkdownText.swift`, `CodeBlockView.swift`.

**Design system — extend to monochrome (§4 M0):**
- `DesignSystem/Theme.swift` (spacing/radius/motion/typography tokens are good; **only the color palette changes**), `DesignSystem/Components.swift` (`StatusPill`, `ConnectionBadge`, `Loadable`/`LoadableView`, `EmptyStateView`, `ErrorStateView`, `NaturalConfirmationSheet`, `Date.saliRelative`).

**Screens — complete, leave alone this pass:** Activity, Tasks, TaskDetail (artifact download/ShareLink is the file-receive engine to reuse), Life, System, Memory, Learning, Settings, Devices, Onboarding.

**Notifications/Push — activate, don't build:** `Features/Notifications/PushManager.swift` (fully built: `requestAuthorization` → `POST /devices/{id}/push-token`) awaits only an app-delegate + entitlement; gate on backend APNs sender.

**Delete as part of cleanup:** `ChatViewModel.ingestNew` (dead), and either wire or remove `EventType.resourceState` (nothing maps to it).

---

## 4. Prioritized Build Plan

Each milestone is independently buildable and on-device testable. Backend touchpoints quote exact A/B names.

### M0 — Monochrome design-system foundation + Sali mark
*Goal: the whole app reads black/white/gray, premium and minimal, before any chat work.*
- **Files:** `DesignSystem/Theme.swift` (replace `accent` blue and all semantic hues with a **grayscale ramp** — e.g. ink/graphite/steel/mist/paper; encode status via *weight/shape/opacity + one restrained tonal cue*, not saturated color, so `ok/warn/danger` degrade to gray glyphs); `DesignSystem/Components.swift` (`StatusPill`, `ConnectionBadge` dot → monochrome; live dot = fill vs ring, not green/red); new `DesignSystem/SaliMark.swift` (a simple, premium vector logo/app-glyph — single-stroke monochrome orb, reusing the existing `SaliOrbIcon` motion language); app icon asset set.
- **Backend:** none.
- **Test:** every screen renders coherent monochrome in light+dark; `ConnectionBadge` states distinguishable without color.

### M1 — Chat hero polish (streaming perf + scroll stability + monochrome bubbles)
- **Files:** `Features/Chat/ChatView.swift` (auto-scroll: pin-to-bottom only when already at bottom; stable `LazyVStack` ids so delta re-parse doesn't jump; monochrome user/assistant bubbles — differentiate by alignment + surface weight, not blue), `MarkdownText.swift`/`CodeBlockView.swift` (monochrome code surface), `Features/Chat/ChatViewModel.swift` (throttle/coalesce `agent.token` appends to reduce re-parse churn).
- **Backend/events:** consumes existing `agent.run`/`agent.token`/`agent.final`; no new wiring.
- **Test:** long streamed markdown+code reply stays smooth, no scroll jank, no double-stream on replay.

### M2 — Per-message send status + in-chat activity disclosure
- **Files:** `ChatViewModel.swift` — add `enum SendStatus {sending, queued, streaming, sent, failed}` to `ChatMessage`; drive transitions from WS: `POST /conversation/message` → `sending`; `message.queued`(matching preview/run) → `queued`; `message.dequeued`/`attention.turn_started`/`agent.run` → `streaming`; `agent.final` → `sent`; `agent.error`/`error`/`intent.revoked` → `failed`. Add an **inline activity trail** built from `agent.status`, `agent.tool`(`name`,`phase`,`ok`,`summary`), `task.tool.started/completed`, `task.progress`, `task.step.completed/failed` (human `summary` only — never CoT/`agent.thinking` text). `ChatView.swift` — per-bubble status glyph (monochrome: dot→ring→check→retry) + tap-to-retry; expandable activity disclosure under the streaming bubble.
- **`SaliEvent.swift`:** add mappings for `message.queued`, `message.dequeued`, `attention.turn_started/completed`, `execution.started/completed`.
- **Backend:** none (pure WS inference — Gap 3).
- **Test:** send while Sali busy → bubble shows `queued` then `streaming`; kill network mid-send → `failed` + retry on the bubble, not a global alert.

### M3 — File send + in-chat file receive
- **Files:** `ChatView.swift` (attach button in input bar → `UIDocumentPicker`; disabled with affordance when no `active_task_id`), `ChatViewModel.swift` (resolve `active_task_id` via `GET /tasks`/`GET /status`; call the now-activated `APIClient.upload(taskId:filename:data:)`; on success post a text message referencing the file per Gap 1 interim), reuse `TaskDetailView` artifact download for **in-chat receive**: on `task.tool.completed`/`agent.tool phase:"done"` for the active task, `GET /tasks/{id}/artifacts` → render an inline downloadable artifact card (reuse `ArtifactRow` + `download()` + ShareLink).
- **Backend:** `POST /api/v1/tasks/{task_id}/files` (raw bytes, `?filename=`, ≤50 MB — **not multipart**, `application/octet-stream`); `GET /api/v1/tasks/{task_id}/artifacts`; `GET …/artifacts/{aid}/download`. Gaps 2/4.
- **Test:** with an active task, send a PDF → appears in workspace; Sali produces an artifact → downloadable card in chat.

### M4 — Message-queue UI
- **Files:** `ChatViewModel.swift` (client outbox: allow composing/sending while `streaming` instead of hard-blocking on `isSending`; track queued sends and reconcile against `message.queued`/`message.dequeued`), `ChatView.swift` (a subtle "Sali is busy — N queued" monochrome header/badge and a queued-messages affordance). Optionally read `queue_size` from `GET /status` if Gap 3 backend nicety lands.
- **Backend:** WS `message.queued`/`message.dequeued`, `attention.message_received`/`_classified`; optional `GET /api/v1/status`.
- **Test:** fire 3 messages while Sali is mid-turn → all show queued with position/order, then process one-by-one matching `dequeued` events.

### M5 — Image send + vision
- **Interim (ships now, no backend change):** `ChatView.swift` add `PhotosUI`/`PHPickerViewController`; downscale/encode; upload via M3's task-file path; post a text message referencing the image. Clearly label as "shared to workspace" (not guaranteed multimodal).
- **Real path (gated on Gap 1):** extend `SendMessageRequest` payload in `ChatViewModel.send()` from `["content":text]` to include `attachments`; render sent images as image bubbles.
- **Backend:** interim = `POST /tasks/{id}/files`; real = multimodal `POST /conversation/message` (Gap 1 — must land server-side first).
- **Test:** interim — image lands in workspace and is referenceable; real — Sali reasons over the attached image in its reply.

### M6 — Sali-initiated messages hardening
- **Files:** `ChatViewModel.swift` (confirm gate on `event_type=="agent.message" && data.channel=="agent_message"`; map `importance`→monochrome emphasis tiers: milestone/completion strong, warning/failure ringed, progress/update quiet; handle `agent.reengagement` as a gentle resurfaced-question chip, `agent.follow_up_suppressed` silently), `ChatView.swift` ("Sali • Proactive" treatment in monochrome), `SaliEvent.swift` (add `agent.reengagement`, `agent.follow_up_suppressed`).
- **Backend:** WS `agent.message`, `agent.reengagement`, `agent.follow_up_suppressed`. (Note: `send_agent_message` currently has no internal caller — proactive messages depend on external wiring; build to receive, verify with a manual trigger.)
- **Test:** injected `agent.message` of each importance renders correctly and lands in both Chat and Notifications.

### M7 — Notifications / replay hardening (+ push when backend ready)
- **Files:** persist `NotificationsView` history (currently in-memory only) to disk; verify `WebSocketClient.subscribeForRecovery()` `after_seq` covers gaps and reconciles the 300-event `liveEvents` ring; add `GET /events?after_seq=` REST backfill on cold start beyond `MAX_REPLAY=500`. **Push (gated on Gap 6):** add `@UIApplicationDelegateAdaptor` in `SaliApp.swift`, instantiate `PushManager`, enable the Settings toggle (`SettingsView.swift:214`) — only once a backend APNs sender exists.
- **Backend:** WS `subscribe {after_seq}` → replay; `GET /api/v1/events?after_seq=&limit=`; `POST /devices/{id}/push-token` (already wired). APNs sender = Gap 6 (server-side, blocks the toggle).
- **Test:** background the app across N events → on resume, exactly the missed durable events replay, none doubled, none lost; ephemeral `agent.token` correctly *not* replayed.

---

## 5. Risks / Unknowns to resolve before each milestone

- **M0:** Confirm status legibility without hue — `ok/warn/danger` must survive as gray glyphs (shape/weight), since the current design encodes meaning in green/orange/red. Decide the one permissible tonal accent (if any) before recoloring 11 screens.
- **M1:** `agent.token` arrives with `sequence: null` and is not idempotent-keyed by seq — confirm the existing `processedEventIDs` dedupe still holds under coalescing, and that throttling never drops the final chunk before `agent.final`.
- **M2:** Correlating `message.queued`/`message.dequeued` to a specific optimistic bubble — the send's `run_id` may differ from the coordinator run id (A §2 flags this), and queue events key on `message_id`/`content_preview`. Resolve the matching key (preview + timestamp?) before building status transitions, or bubbles will mis-attribute.
- **M3:** The `active_task_id` may be null (no open task) — decide the UX when a user attaches with no task (disable vs. auto-create). Confirm 409 handling (task with no bound `workspace_root`). Verify `X-Filename` vs `?filename=` precedence with the real server.
- **M4:** Without a REST queue endpoint, position/ordering is inferred from event order only — confirm the WS stream delivers `message.queued` for *foreground* sends (some categories like `queue_for_later` differ). Don't display a false "position N."
- **M5:** Blocked on Gap 1 for real vision — get a server commitment on the `attachments` contract shape before building the real path; ship interim clearly labeled so users aren't misled that Sali "sees" the image.
- **M6:** `send_agent_message` has **no internal caller** today — proactive messages may not fire at all until external wiring exists. Confirm a trigger source before promising the feature; build receive-side regardless.
- **M7:** APNs has **no sender** — do not enable the push toggle or promise background delivery; WS notifications only work while connected. Confirm `MAX_REPLAY=500` + `GET /events` backfill covers long offline windows, and that the 5-connection cap (4029) doesn't strand a reconnect during token refresh.

---

**Bottom line:** the Core and WS layers are already a faithful single-runtime client — the work is concentrated in Chat and in a monochrome reskin. Nothing in this plan invents an endpoint or event; the only true blockers (image/vision, APNs, queue-depth REST) are named as backend gaps with concrete, one-mind-preserving proposals, and every milestone ships value without waiting on them.