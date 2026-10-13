# Sali Transformation Plan — llama.cpp Migration & Ubuntu Install

**Source:** MASTER DIRECTIVE (transform, repair, install Sali on Ubuntu; replace Ollama with llama.cpp).
**Started:** 2026-10-13. **Host:** Ubuntu 26.04.1 LTS, i9-11900K (AVX-512), 14 GiB RAM,
RTX 2080 Ti 22,528 MiB (driver 595.99.02), 1.7 TiB free.

## Fixed targets (from the directive — not negotiable)

- Primary conversational model: **Qwen3.5-35B-A3B** (no fallback, no silent substitute).
- Inference engine: **llama.cpp** (replacing Ollama).
- OS: Ubuntu Desktop. Kali assumptions removed.
- iOS app preserved; updated only where the backend contract changes; Mac build later.
- One mind: no second conversational model, no agent swarm.
- Ollama removed only after llama.cpp passes all migration gates.

## Workstreams

### WS-A — Model & engine preparation
1. Verify model identity/artifact — **DONE** (see decisions log).
2. Download artifact + mmproj, verify sha256 — in progress (background).
3. Install llama.cpp (prebuilt cudart binary) — in progress (background).
4. Isolated inference test; measure GPU offload, TTFT, tok/s, memory.
5. Verify incremental streaming, tool calling, vision (mmproj).

### WS-B — Audit & repair (concurrent with WS-A)
1. Test baseline on Ubuntu (`.venv`, pytest `-m "not live"`).
2. Ubuntu compatibility: bootstrap script, docs, service files; remove Kali-only shim script.
3. `LlamaCppProvider` behind `sali.provider.base.ModelProvider` (registry + settings extension).
4. Identity/self-state text made engine-agnostic; twin model inventory; vram-guard retargeted.
5. Keep `ModelProvider` contract: chat, chat_stream, embed, describe_image, count_tokens,
   context_limit, health + one-generation-at-a-time lease + GPU gate.

### Phase gates (directive §12)
- **P1 Discovery** — done (audit report in progress file).
- **P2 Model prep** — running.
- **P3 Architectural repair** — identity/lifecycle already largely exists (kernel, working memory,
  attention, executive, life loop, initiative). Work = verify, repair defects, remove
  coding-agent framing where it defines identity (audit says: mostly prompt-text level).
- **P4 Ubuntu + runtime integration** — DB done; provider adapter; services.
- **P5 iOS compatibility** — inspect networking; update only if API contract changes; document Mac build.
- **P6 Integration tests** — full suite + live gates.
- **P7 Installation** — systemd units, power envelope, vram guard, health checks, final report;
  Ollama removal only after all gates green.

## Open constraints discovered
- 14 GiB RAM total — no fat conversions; GGUF direct download only; embeddings stay CPU.
- No cmake/CUDA toolkit on host → prebuilt cudart-bundled llama.cpp binary (no toolkit needed).
- Repo was not a git repository (plain copy) — no VCS safety net; changes are journaled instead.
