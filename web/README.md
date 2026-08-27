# Sali Web — a window into the live runtime

A single-screen, real-time operating environment for Sali. Not a dashboard *around* Sali — a window
*into* it. Every visual change is caused by a real runtime event; nothing loops on a timer.

- **Stack:** Vite + React + TypeScript + Tailwind, Three.js / React-Three-Fiber for the neural graph,
  Zustand (transient store) + TanStack Query (snapshots). No second brain, no duplicated agent logic.
- **Backend:** the SAME Sali. The browser is a client of `sali serve` (FastAPI): `/ws` (chat turns),
  `/stream` (the durable event firehose), `/api/*` (snapshots). All redacted at the boundary (§34).

## Run it

```bash
# 1. build the SPA (served same-origin by Sali's FastAPI)
cd web && npm install && npm run build

# 2. serve Sali's API + the built cockpit on one origin
sali serve --host 127.0.0.1 --port 8790
# open http://127.0.0.1:8790/
```

### Dev (hot reload)

```bash
sali serve --host 127.0.0.1 --port 8790   # backend
cd web && npm run dev                       # Vite on :5173, proxies /api /ws /stream to :8790
# open http://localhost:5173/
```

## Shape

`src/websocket/` — the two live surfaces (chat `/ws`, firehose `/stream`), auto-reconnect + seq-resume.
`src/stores/store.ts` — the one client source of truth both streams feed.
`src/lib/` — typed REST client + wire types (mirror `sali/api` serialization).
`src/components/{brain,world,activity,attention,tasks,chat,memory,system,ui}/` — each panel is a slice.

The brain is the visual center: it renders Sali's actual knowledge graph (degree-ranked core, tool
leaves collapsed), and lights the REAL nodes/edges a genuine retrieval used — never invented activity.
