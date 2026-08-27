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
_(appended as increments land)_
