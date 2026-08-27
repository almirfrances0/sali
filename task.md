# SALI — task.md · closing the gap to `sali.md` (the master vision)

Built from a full read of `sali.md` (94 sections + the "Attention + World-State Engine" addendum) and a
grounded audit of the **live** system. Governing rule (`sali.md` §92/§93): **inspect first, never
duplicate — build ON what exists, add only real gaps, incrementally, every subsystem tested.**

This is a private, $10B-tier local intelligence: every increment uses deterministic-first logic, keeps
the model warm but wakes expensive reasoning only when attention decides it matters, and enforces every
boundary outside the LLM.

---

## A. What already exists (mature — DO NOT rebuild; build on it)

| Spec area | Status | Where it lives |
|---|---|---|
| Warm model, streaming, pooled conns, systemd daemon, startup (§2/§3/§49/§50) | ✅ | `provider/ollama.py` (`keep_alive="30m"`), `db/pool.py`, `kernel.py`, `cli/main.py::_daemon` |
| Memory: episodic/semantic/procedural/environment/identity, temporal graph, consolidation, confidence+provenance+freshness, contradiction, embeddings, decay, hybrid+graph retrieval, scopes (§36–44/§60/§61/§72) | ✅ | `memory/`, `graph/`, `learning/`, `retrieval/`, `context/engine.py` |
| Desktop twin + world graph (machine/hw/services/network/projects/containers → graph) (§5/§35/§40) | ✅ | `twin/observers.py`, `twin/sync.py`, `graph/` |
| Perception engine: fs-watch + window + AT-SPI a11y + vision, noise-filter, importance score, burst-aggregate, bounded recent buffer, DB sink (§8–11/§63) | ✅ (partial fusion) | `events/` (engine/importance/aggregate/fswatch/sink), `perception/` (atspi/activewindow/vision) |
| Tool Intelligence: discovery→capabilities→authority→experience→daemon→retrieval→CLI→selection→enforcement (§23/§26/§77) | ✅ | `twin/{tools,capabilities,authority,tool_intel,selection,interpret,tool_report}`, `learning/tool_experience`, `core/toolvocab`, `sali tools` |
| Execution broker + policy (§26/§27), 52 tools, sandboxed exec | ✅ | `tools/dispatch.py`, `security/policy.py`, `tools/exec.py` |
| Email + calendar (§31–33), SSH + VPS (§34) | ✅ | `comms/`, `tools/builtins/{comms_tool,ssh}` |
| Secret vault (Fernet) + redaction at every boundary (§29/§64/§65) | ✅ | `config/vault.py`, `config/secrets.py`, `security/redact.py` |
| Error learning, procedures, episodes, consolidation, GC, backup (§42/§57/§58/§68) | ✅ | `learning/`, `memory/retention.py`, `db/backup.py` |
| Streaming WS events, terminal-first, UI-independent backend (§12/§80/§81–83) | ✅ | `api/app.py`, `cli/main.py` |

---

## B. The gap (what `sali.md` asks for that is NOT built)

**Centerpiece (the "one thing I would add"):** a live **Event Bus → World-State → Attention** loop with a
persistent **Self-State** — turning "a warm model in VRAM" into a present, self-aware intelligence that
wakes reasoning only when something matters and can speak up proactively.

Verified gaps: Self-State/self-knowledge (§6/§7/§41/§71) · World-State object (§5/§18/§72/§73) · tiered
Attention + wake-decision (§14) · generalized Event Bus + fusion sink (§13/§89) · proactive loop
(§16/§74–78) · live health + online/offline (§51/§52/§53) · sudo privilege broker (§28/§30) · learning
queue + curiosity + budgets (§45/§46/§79) · baselines/anomaly (§18/§19/§78) · environmental timeline
(§62) · security environmental intelligence (§24/§25) · explicit self-reflection (§57).

---

## C. Build plan — phased, incremental, each a tested commit

### PHASE 1 — The Living Core (Attention + World-State + Self-State) · the centerpiece

- **1 · Sali Runtime Self-State (§6/§7/§41/§71/§84).**
  A persistent, continuously-updated self-model. New `sali_state` singleton (JSONB, survives reboot) for
  the genuinely-new volatile fields (mode, current_focus, active_operation, last_success/failure,
  counters) + a `SelfState.assemble()` that COMPOSES the full view from existing stores (identity memory,
  `tasks`, `needs_grounding` = uncertainties, subsystem health) — no duplication. Static self-knowledge
  ("my brain is Qwen via Ollama; graph = long-term relations; …"). Loop updates it each turn; surfaced in
  context so Sali can answer "who are you / what are you doing / what are you unsure about / how do you work".
  *Build on:* `tasks/`, `memory/diagnostics`, `context/engine`, `runtime/loop`.

- **2 · World-State snapshot (§5/§18/§72/§73).**
  A live "what is happening now" read-model: focused app/project (perception), recent important
  observations (`events` engine `recent()`), recent commands/errors + changed files, active task, twin
  structural highlights, health + online/offline. Assembled on demand (cached briefly). Injected into
  context assembly so a turn ALREADY knows the environment (§73) — "why isn't this working?" needs no cold
  inspection. *Build on:* `events/engine.recent`, `perception/service`, `twin`, `context/engine`.

- **3 · Attention Engine (§14/§15).**
  Promote the deterministic `importance.score` into a real attention layer: classify each observation
  `routine / interesting / important / critical`, then DECIDE `ignore | record | investigate | notify`
  (deterministic policy; LLM only on `investigate`). Wired into the perception pipeline. Bounded, budget-
  aware — never wakes the model on routine churn. *Build on:* `events/importance.py`, `events/sink.py`.

- **4 · Event Bus generalization + fusion (§13/§89).**
  An in-process pub/sub `EventBus` unifying desktop events (existing engine) with cheap **system** events
  (process start/stop, service state, listening ports, tool install) via new lightweight deterministic
  watchers → one pipeline → attention → world-state → durable `event` table (reuse it). Fusion sink also
  folds structured signals into twin/memory where meaningful. Diff-driven, no event flooding (§57).
  *Build on:* `events/engine`, the `event` table, `twin/sync` diff discipline.

- **5 · Proactive loop (§16/§74–78/§46).**
  Attention `important/critical` → a bounded, de-duped proactive surfacing to Almir over the WS channel
  ("build failed — port 3000 occupied"; "you changed nginx.conf but didn't restart it"; "this looks like
  the incident we fixed before"). Observation ≠ action: it INFORMS, never auto-acts, unless policy says so.
  *Build on:* `api/app.py` (sense/notify channel), `tools/builtins/notify_tool`, memory recall.

### PHASE 2 — Awareness & Safety

- **6 · Health model + online/offline (§51/§52/§53).** Live `SubsystemHealth` (model/graph/memory/
  perception/fswatch/tools/email/internet), continuously updated, exposed to World-State + reasoning; a
  cheap connectivity probe so Sali never claims to have researched while offline; systematic degraded
  modes. *Build on:* `provider.health`, `memory/diagnostics`, `cli::doctor`.
- **7 · Sudo privilege broker (§28/§30).** A SUDO_ASKPASS helper that fetches the sudo password from the
  vault at exec time and feeds it to sudo via a secure fd/env — the secret NEVER enters the model,
  context, logs, or memory. Gated by the existing authority/policy. *Build on:* `config/vault`,
  `tools/exec`, `security/policy`, tool-authority (sudo already = ELEVATED).
- **8 · Learning queue + curiosity + budgets (§45/§46/§79).** A durable `learning_queue` (unknown tool /
  repeated failure / knowledge gap → agenda item) the daemon works off UNDER resource budgets
  (CPU/inference/time), never infinite autonomous experimentation. *Build on:* `learning/`, daemon
  faculties, `events` attention.

### PHASE 3 — Environmental Intelligence

- **9 · Baselines + anomaly detection (§18/§19/§78).** Learn "normal" (processes/services/ports); flag
  deviations → attention. *Build on:* twin, event bus, attention.
- **10 · Environmental timeline (§62).** A queryable "what happened today?" over the event log.
- **11 · Security environmental intelligence (§24/§25).** Reason about exposures/weak configs tied to the
  live machine (new listening port, world-writable sensitive file). *Build on:* twin, anomaly, capability vocab.
- **12 · Explicit self-reflection (§57).** Post-task reflection → lessons → memory, throttled. *Build on:*
  `learning/consolidate`, self-state.

---

## D. Invariants (hold on EVERY increment)

- Deterministic-first; the LLM is woken only when attention/reasoning genuinely needs it (§14/§79/§89).
- Every boundary enforced OUTSIDE the model (§28/§91). Secrets never enter context/logs/memory (§28–30/§65).
- Local-first; nothing private leaves the machine except through an explicit tool (§1/§64).
- No prompt-bloat / no faked behavior — build mechanisms, not system-prompt claims (§56/§86/§91).
- `make ci` green (ruff + import-linter + mypy --strict + 85% coverage), memory benchmark 9/9, live-verified,
  service restarted, each increment its own commit. Reuse; never duplicate (§92).
