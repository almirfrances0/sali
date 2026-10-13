# Sali Web — build plan (a window into the live runtime)

**Principle:** the browser is a *second interface* to the same Sali. No second brain, no fake data,
no timers. Every visual change is caused by a real `event`. Backend logic is never duplicated — the
web layer only *exposes* what already exists. Decisions: **Vite + React + TS**, static-built and served
by Sali's own FastAPI (one process, one origin); **R3F 3D brain** + 2D detail-on-click; localhost-trust
(no added auth gate — freedom, not restricted).

## The two live surfaces (mental model)
1. **Chat stream** — `/ws` → `AgentLoop.astream()` → `LoopEvent{kind,text,data}`. Already redacted &
   browser-ready. Per-connection; only the turn this client submits.
2. **Firehose** — durable `event` table + `pg_notify('sali_events', seq)` + in-proc `EventBus`. ~37
   event_types across all subsystems. **No browser transport yet** → the main thing to build.

---

## Phase A — backend web layer (additive; respects layering, mypy --strict, import-linter)

- [ ] **A1 · real deltas on the bus** — one-line `event` emissions so the brain animates truth, not guesses:
  `graph.node_upserted`/`graph.edge_upserted` (graph/writer.py), `self.presence` on mode change
  (runtime/self_state.py), `task.created`/`task.step_advanced`/`task.finished` (tasks/store.py), and a
  run-start `LoopEvent{kind:'run', data:{run_id}}` (runtime/loop.py). Tests each. Rides the existing bus.
- [ ] **A2 · read-model + serializers** — `api/serialize.py` (UUID/datetime/enum/dataclass → JSON, always
  `redact_obj` at the boundary). `to_dict()` where missing (WorldState, Node/Edge, MemoryHit view).
- [ ] **A3 · REST snapshot endpoints** on the same FastAPI app (`api/routes/*`): `/api/health` (real
  HealthService), `/api/self`, `/api/world`, `/api/presence`, `/api/attention/counts`,
  `/api/graph/snapshot|node/{id}|neighborhood`, `/api/memory/search|audit`, `/api/events?since=`,
  `/api/tasks`, `/api/schedules`, `/api/learning/queue|procedures`, `/api/tools|tools/executions`,
  `/api/runs|runs/{id}/events`.
- [ ] **A4 · event fan-out bridge** — `WS /stream`: ONE shared `LISTEN sali_events`; on notify,
  `SELECT * FROM event WHERE seq > watermark`, redact, fan out to all clients; client resumes by `seq`.
  Presence derivation adapter (agent_runs.state + Health.internet + desktop.observed → ONLINE/OBSERVING/
  EXECUTING/THINKING/WAITING/LEARNING/IDLE).
- [ ] **A5 · serve the SPA** — CORSMiddleware (localhost origins) + StaticFiles mount of `web/dist`;
  make `--host` the app default path. Kernel exposes shared pool/EventBus to the API.

## Phase B — frontend scaffold (`web/`)
- [ ] **B1** — Vite + React + TS + Tailwind; build → `web/dist` served by FastAPI. Design tokens (dark,
  minimal, premium, scientific — not cyberpunk).
- [ ] **B2** — `websocket/useSaliStream` (auto-reconnect, backpressure, seq-resume) + Zustand event store
  (bounded buffers) + TanStack Query REST client + generated `types/`.
- [ ] **B3** — single-screen cockpit shell: CSS grid, no sidebar, no scroll at 3440×1440, responsive down
  to laptop; contextual expand/focus, nothing disappears.

## Phase C — components (each a slice of the one event store)
- [ ] **C1 · Brain** (R3F) — nodes=confidence/dim=stale; real retrieval lights real edges; LOD +
  neighborhood; click→2D inspector; calm when quiet.
- [ ] **C2 · Chat** — one continuous conversation over `/ws` (token/thinking/tool/final/sense/confirm).
- [ ] **C3 · Presence + Current Operation + Attention** — derived state, tier counts, live transitions.
- [ ] **C4 · Activity stream** — the firehose, elegant, click→details in place.
- [ ] **C5 · World view** — twin inventory + live gauges; process/service/file changes as they happen.
- [ ] **C6 · Tasks / Tools / Memory inspector / provenance ("Why does Sali know this?")**.
- [ ] **C7 · Proactive** — `sali.proactive` → in-UI card (Explain/Inspect/Dismiss).

## Phase D — verify (after each major component)
tests + mypy --strict + import-linter green · reconnection · **no fake production data** · 60fps under
load · quiet-when-idle · terminal/web parity.

## Verification log
- **Phase A DONE (backend web layer)** — A1 deltas + A2 serializer + A3 REST + A4 fan-out + A5 serving.
  `make ci` green (515 passed, 87.37% cov, mypy --strict + import-linter kept). Live-verified against the
  REAL `sali` DB via `sali serve --host 127.0.0.1 --port 8790`:
  - `/api/self` → real identity + env (machine Kali, model sali:latest, workspace/source from graph).
  - `/api/presence` → derived live state (caught a daemon run mid-turn: `reason_plan` → "thinking").
  - `/api/graph/snapshot` → connected core: 67 nodes / 33 edges, Sali+Kali hubs, 2477 ext_tool leaves
    collapsed into a cluster (LOD) — a real brain, not a hairball.
  - `/api/attention/counts` → real tiers (critical 1, important 270, interesting 100, routine null).
  - `/stream` (live WS) → smoke event arrived with `api_key` **redacted**, plus a real daemon `tool.failed`
    — the genuine firehose, redacted at the boundary (§34).
  - TODO (Phase D): a systemd unit for `sali serve` (prod runs the daemon, not the API yet).
- **Phase B + C DONE (frontend cockpit)** — Vite+React+TS+Tailwind under `web/`, R3F brain, Zustand store
  fed by both live streams, TanStack Query snapshots. `npm run build` + `tsc --noEmit` green (0 errors).
  Live-verified via headless Chromium against the running backend:
  - The full cockpit renders on ONE screen (no scroll): TopBar (SALI ● Thinking — a real daemon run),
    World (real machine/kernel/model/workspace + live commands/files), Attention (real tiers), Activity
    (honest quiet state), Tasks, Chat.
  - **The brain renders the REAL graph in WebGL** — Kali as the central hub wired to its models, running
    services (docker/ssh/postgresql/ollama), and projects; degree-ranked core, 2477 tools collapsed.
  - **`/ws` live round-trip proven**: typing "what postgres runs here" surfaced a real retrieval hunch
    ("Services running: postgresql (running), ollama (running), docker…") — no model turn, no console errors.
  - Components built: Brain (data-driven activation on real retrieval frames), Chat (continuous session,
    tool/thinking/token stream, sense hunches, confirm modal), Activity (firehose, click→inspect), World,
    Attention, Tasks, Presence, Memory/node Inspector (provenance "why Sali knows this"), Proactive toasts.
  - Remaining polish: a dedicated Tools catalog panel; brain neighborhood-expand on node click; ARIA pass;
    prod systemd unit for `sali serve`.

## Canonical self/home/environment identity (correction directive §1-§15)
- **DONE + live-verified** — "this host IS my home/body" is now structural, IDENTITY/SELF/WORLD are
  distinct, live beats stale, system queries prefer current reality. (commit "Canonical self/home identity")
  - Graph: `machine:kali` marked `is_self=true, role=home`, stable `/etc/machine-id`, `agent:sali
    --lives_on--> machine`. Live-confirmed via psql + `/api/self` (is_home:true, machine_id set).
  - Context: self-state + health now injected EVERY turn (were tool-only), distinct from identity/world;
    for a system_query the semantic-memory pool is demoted below the live band. IDENTITY no longer
    hardcodes the OS (drift-prone) — the grounded self_note names the real machine.
  - Retrieval: `system_query` flag + `_SYSTEM` cues force needs_live; `_to_hit` gives observed claims
    read-time precedence over inferred ones.
  - **Live e2e**: `sali agent "what computer am I on, and what kernel?"` → "This is Kali GNU/Linux Rolling
    … kernel 7.0.12+kali-amd64 … It's my home machine (your machine)." Answered LIVE 7.0.12, NOT the
    seeded stale "6.13.0-old" memory. Context event: included=[identity,security,self,health,…,world,…,
    memories] — self/health present, memory demoted last. Daemon + serve restarted on the new code.
- **Remaining follow-ups (noted, not yet built):**
  - S6 remote/local: a dedicated GraphSink primitive to mint `host:{slug}` nodes + `agent:sali
    --can_access--> host` on ssh (so a VPS is a distinct entity, never merged with the home machine).
    Core invariant already holds: only the local machine is is_self/home; remote state isn't merged into
    local world-state.
  - S7 tool provenance (§10): stamp tool results with host_id + observed_at + source-type.
  - World-state resource fields (§7): fold live gpu/ram/disk/network/services/processes into
    WorldState.snapshot so system queries get them proactively (today Sali fetches them via tool calls).
