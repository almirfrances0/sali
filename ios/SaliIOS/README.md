# Sali — iPhone Control Center

The premium, native iOS control center for **Sali**, Almir's local-first personal AI agent. Not a dashboard,
not a chat wrapper — a calm, real-time window into Sali: talk with it, watch what it's doing, manage tasks
and memory, review learning, monitor the machine's health, and stay reachable wherever you are.

> **One Sali, many interfaces.** The terminal and this app share the same Sali instance, session, and durable
> state. The app is a *secure window* into Sali — never a second task system, never a bypass around Sali's
> safety, workspace, or task authority.

## Architecture

```
Sali backend (Kali) ──▶ Cloudflare Tunnel ──▶ https://sali.salieno.com
                                                     │
                            REST /api/v1 (state) ────┤
                            WebSocket /ws (real-time)─┘
                                     │
        ┌────────────────────────────▼─────────────────────────────┐
        │  SwiftUI · async/await · MVVM · no third-party deps        │
        │                                                            │
        │  App          composition root, routing                    │
        │  Core/Config  environments + base-URL/WS derivation        │
        │  Core/Auth    Keychain · TokenStore · AuthService          │
        │  Core/Net     APIClient (actor, auto-refresh on 401)       │
        │  Core/WS      WebSocketClient (auth · after_seq recovery)  │
        │  Core/Realtime SaliEvent stream model                      │
        │  DesignSystem theme + state-complete components            │
        │  Models       typed API responses                          │
        │  Features     Chat · Activity · Tasks · Life · System ·    │
        │               Memory · Learning · Notifications · Settings │
        └────────────────────────────────────────────────────────────┘
```

Layers are decoupled: screens depend on `AppState` / `APIClient` / models, never on raw HTTP. The app
survives network loss, WebSocket disconnect, token expiry, and server restart, and recovers missed events by
sequence on reconnect.

## Security model (summary)

- **Device enrollment (one-time):** a fresh install has no authority. Pair it once with a code minted on the
  host (`sali enroll-code`). The code is single-use and expires.
- **Device-bound sessions:** short-lived access token + rotating refresh token, stored only in the iOS
  Keychain. The server stores only hashes; the owner can revoke any device instantly.
- **Roles:** owner ⊃ controller ⊃ observer (observers are read-only).
- **Defense in depth:** Cloudflare Tunnel + WAF is one layer; the API authenticates/authorizes every request
  itself. No DB/shell/`/run` endpoints — only structured operations.

## Key rules

- **Thin client** — no agent reasoning, memory, or task logic on the device; the backend is authoritative.
- **Real events only** — streaming is real (WebSocket), never faked; motion communicates state.
- **Never show chain-of-thought** — only high-level activity ("Thinking", "Checking files").
- **REST for state, WebSocket for real-time** — reconnect recovers missed events by `sequence`.

## Getting started

This project was authored on Linux and **must be built on macOS in Xcode** (it cannot be compiled/signed on
the Kali host). See:

- `docs/API_REFERENCE.md` — the complete backend contract (auth, WebSocket, events, tasks, files, system…).
- `docs/XCODE_HANDOFF.md` — how to create the Xcode app target, sign, and run.
- `docs/CLOUDFLARE.md` — how to expose the API securely at `sali.salieno.com`.

Quick path: open in Xcode 15+, create an iOS 17 App target embedding these sources, set your team, run
`sali serve` on the host, `sali enroll-code` to get a pairing code, and enter it in the app's onboarding.
