# Sali Transformation — Engineering Progress Journal

Durable checkpoint. Update after every phase and before any disruptive operation.
Newest entries last. Timestamps are host-local.

---

## 2026-10-13 — Phase 1: Discovery & baseline (COMPLETE unless noted)

### Verified host facts (measured, not assumed)
- Ubuntu 26.04.1 LTS (Resolute Raccoon); Python 3.14.4; gcc 15.2.0; git 2.53.0.
- CPU: i9-11900K, 16 threads, AVX-512 (avx512bw/cd/dq confirmed).
- RAM: 14 GiB total, ~9 GiB available at idle.
- Disk: 1.7 TiB free on / (NVMe).
- GPU: RTX 2080 Ti, 22,528 MiB reported by nvidia-smi (modded 22 GiB card is REAL at driver level).
- Network: huggingface.co + github.com reachable.
- cmake/nvcc absent; ensurepip absent (worked around with `venv --without-pip` + get-pip.py).

### Verified repo facts
- Not a git repository — no VCS history; plain file copy from the Kali machine.
- `pyproject.toml` declared `readme = "README.md"` but the file was missing → broke `pip install -e .`
  **FIXED:** created README.md (minimal, accurate).
- INSTALL.md references `sali.md` and `docs/ONE-MIND.md` — both absent from the tree (stale refs).
- Nine `.bak-*` files litter `src/`; macOS AppleDouble files (`._*`) litter `ios/` (would be
  ingested by xcodegen globs — removal queued in iOS phase).
- errors.md (2026-09-18) documents: import-linter layer contract broken in 5 modules; 3 failing
  tests (`test_phase6_proactive` ×2, `test_prompt6_capstone` ×1); uncommitted edits.
  → Baseline test run on Ubuntu will confirm which of these still hold.

### Environment actions performed
- PostgreSQL 18.6 + postgresql-18-pgvector 0.8.1 installed (apt).
- `scripts/bootstrap_db.sh` run: role `almir` (peer auth), DBs `sali`, `sali_test`,
  `sali_restore_tpl`; extensions vector/pg_trgm/pgcrypto present in `sali` (verified via psql).
- `.venv` created; `pip install -e ".[dev]"` OK; `sali --help` works.
- Sudo: password supplied by owner verbally in-session (not written anywhere). Old Kali DB backup:
  none — owner chose FRESH START (`sali init` when runtime is ready).

### WS-A — Model & engine (in progress)
- Model verified: `Qwen/Qwen3.5-35B-A3B` — official repo, Apache-2.0, arch `qwen3_5_moe`,
  pipeline `image-text-to-text` (multimodal → vision supported via mmproj).
- Artifact selected: `unsloth/Qwen3.5-35B-A3B-GGUF` → `Qwen3.5-35B-A3B-UD-IQ4_XS.gguf`
  - size 17,486,174,848 B; LFS sha256 `745def087c9ec34d12a94eb9b31eb52dba958b36def510b7e8f6f2e6bf1be123`
  - rationale: 17.5 GiB leaves ~6 GiB VRAM for KV cache + desktop on the 22 GiB card;
    Q4_K_M (22.0 GiB) leaves ~1.5 GiB → would force CPU offload + RAM we don't have.
  - vision projector: `mmproj-BF16.gguf`, 902,822,592 B, sha256
    `abe81a7212be307a7723ab47a51a87e5c46d0622273ccb04a6a6feba18b21d63`
  - downloading to `~/.local/share/sali/models/` (background; ~4.3 MB/s observed).
- llama.cpp: prebuilt `cudart-llama-b11576-bin-ubuntu-cuda-12.8-x64.tar.gz` (b11576, 2026-10-12)
  downloading to `~/.local/opt/`; cudart-bundled → no CUDA toolkit install required;
  cuda-12.8 chosen (sm_75 Turing supported; driver API 13.2 backward-compatible with 12.8 runtime).

### Ollama coupling map (for the adapter phase)
Backend: `provider/ollama.py`, `provider/residency.py` (/api/ps), settings ModelSettings,
pyproject dep + import-linter list. Operational: `scripts/vram_guard.py` (Ollama HTTP),
`systemd/ollama-sali.conf`, `sali.service After=ollama.service`, CLI daemon model-restore
(duck-typed, degrades gracefully), API model switcher (duck-typed), twin `ollama list`.
Semantic: IDENTITY text in `context/engine.py` + `runtime/self_state.py` ("Ollama ... killing
Ollama kills me"); presets emit Ollama option names. Overflow detection
(`context_budget.is_context_overflow`) is already backend-agnostic — no change needed.
Tests importing `sali.provider.ollama`: test_host_survival, test_single_model_load, test_provider_gpu.

### Blockers / notes
- None currently. Test baseline running.

---

## 2026-10-13/14 — WS-A complete: llama.cpp + Qwen3.5-35B-A3B live-verified

### Artifacts (integrity proven)
- `~/.local/share/sali/models/Qwen3.5-35B-A3B-UD-IQ4_XS.gguf` — sha256 MATCHES HF LFS oid.
- `~/.local/share/sali/models/mmproj-BF16.gguf` — sha256 MATCHES.
- `~/.local/share/sali/models/nomic-embed-text-v1.5.Q8_0.gguf` (142,495,744 B) — embeddings.
- llama.cpp b11576 prebuilt `ubuntu-cuda-12.8-x64` at `~/.local/opt/llama.cpp/` —
  `llama-server --version`: 0.6.0-dev (build 11576, commit 358a420ec); CUDA0 RTX 2080 Ti seen.

### Measured (live, this host, power envelope already applied: CPU 95/125W, GPU 100W)
- Model resident on GPU: 18.8 GiB VRAM of 22.5 (weights 16.3 + KV q8_0 49K ctx + buffers); ~3.7 GiB free.
- Prefill: **766 tok/s** on a 7,018-token prompt (vs 375–690 tok/s on the old Ollama split setup).
- Generation: **41 tok/s** steady state (fully on-GPU; old setup was split GPU/CPU).
- Streaming: TTFT **0.22 s**, genuinely incremental (77 chunks / 59 words / 2.1 s total).
- Tool calling: native, correct name+args (`get_weather{"city":"Paris"}`).
- Vision (mmproj): correct description of a synthetic test image, 6.2 s.
- First-ever request after server start is slow (cold mmap) — steady state is what counts (above).
- Server start to /health 200: ~35 s (16.3 GiB model load + graph build).

### Provider adapter — live integration gates ALL PASS
`src/sali/provider/llamacpp.py` (new) behind `ModelProvider`; shared lease extracted to
`src/sali/provider/lease.py` (semantics unchanged; ollama.py re-exports). Verified live:
health, chat (PONG, real token counts, thinking=None), chat_stream (43 chunks, usage),
tool call via provider, embed 2×768 on CPU server, residency probe REFUSES on model mismatch.
Settings: provider Literal + llamacpp_host:8083 / llamacpp_embed_host:8084 / alias "sali".
Default provider stays "ollama" until the cutover gate (Phase 7).
Provider-scope tests: 89/89 green. Full baseline: 1966 passed, 2 env failures
(power-envelope — FIXED by applying scripts/power_envelope.sh; git-repo — FIXED by git init).

### Running now (transitional, started by hand — systemd units next)
- 127.0.0.1:8083 chat+vision server (GPU, -ngl 99, -c 49152, -fa on, -b 2048, -t 8, --parallel 1,
  KV q8_0, --jinja, --alias sali)
- 127.0.0.1:8084 embeddings server (CPU, -ngl 0, -t 4, -c 8192, --alias nomic-embed-text)

## 2026-10-14 — Phases 4–7: integration, acceptance, installation

### Guard race fix (root-caused live)
sali-vram-guard killed the chat server TWICE: llama.cpp puts even a CPU-only (-ngl 0) server on the
GPU process list (~184 MiB CUDA context), so chat+embed read as "two servers" → guard shed the newest.
Fixes, both verified: (1) `CUDA_VISIBLE_DEVICES=` on the embed unit → embed no longer touches the GPU;
(2) guard server identity moved from process name to /proc cmdline marker (SALI_VRAM_SERVER_MARKER),
plus a hog threshold (1 GiB) so small permanent desktop clients (IDE/compositor/remote-desktop)
never trigger. Regression tests added (`test_the_embed_server_is_not_a_second_chat_server`,
`test_small_desktop_clients_never_trigger`). Guard now logs `ok`, chat server survives.

### Truthfulness pass
Served alias is the model's real name: `--alias Qwen3.5-35B-A3B` (was "sali"). thinks_with edge,
/api/v1/status model field, agent header, and Sali's own answer all name it. Stale
`model:sali:latest` node closed. twin/service.py resolves the configured model name per-backend;
/api/v1/status reads the provider's active model, not static config. IDENTITY/SELF_KNOWLEDGE text
is engine-agnostic ("a local model server"), so no backend swap can make Sali describe himself wrong.

### iOS verdict (Phase 5)
NO code change required: ModelInfo decodes optional ollama-era fields; the switcher is structurally
disabled when the only listed model is active. Two comment strings updated (Ollama→backend).
3362 macOS AppleDouble/`._*` files + .DS_Store removed from ios/ (would have been ingested by
xcodegen globs). Mac build still required later; ios/docs has the references.

### Acceptance gates (all measured live)
- Final suite: **1966 passed, 0 failed, 5 skipped** (baseline was 1966/2 env failures; both env
  causes fixed: power envelope applied, git repo initialized).
- mypy clean on all new/changed provider files. ruff clean. lint-imports: only the PRE-EXISTING
  violations documented in errors.md (untouched by this migration — separate debt).
- /api/v1/status model=Qwen3.5-35B-A3B; /api/v1/models → one fixed model, vision+tools true;
  POST /api/v1/model → 409 "one model, no substitutes" (verified).
- Daemon turn end-to-end: header + Sali's self-report both name Qwen3.5-35B-A3B.
- sali status/doctor: postgres ok (migration 0066), llamacpp ok, one mind alive.

### Installed (systemd, enabled at boot)
sali-power-envelope, sali-llamacpp (chat+vision, :8083), sali-llamacpp-embed (CPU, :8084),
sali-vram-guard, sali (daemon). Ollama server software REMOVED (/usr/local/bin/ollama +
/usr/local/lib/ollama; there was no ollama service, user, or model cache on this box).
Hygiene: 11 unreferenced .bak files removed; git repo initialized (no commits made).

### Deliberately deferred (with reasons)
- ollama.py provider module + ollama pip dep: kept as the selectable-but-inert transition backend.
  Removing it requires reworking 3 test files that pin its safety semantics (runner fingerprint,
  residency survey) — one careful follow-up, not an end-of-session edit. Rollback path stays intact
  until the new stack has production time.
- Pre-existing layer-contract violations (errors.md §2.1) and the webtask.md Phase-D items.
- APNs push credentials (owner's Apple team/key), cloudflared tunnel, SearXNG container —
  owner steps documented in INSTALL.md.

## 2026-10-14 — Cloudflare tunnel live
- cloudflared 2026.10.0 installed; tunnel `sali` (id 7e83657e-10f1-4eb7-bade-9bc79af00604).
- sali.salieno.com CNAME → tunnel (stale Kali-era record overwritten via --overwrite-dns).
- Config at /etc/cloudflared/config.yml (+ ~/.cloudflared copy): ingress → http://127.0.0.1:8080,
  edge-ip-version: "4" (quoted — 2026.x requires string; unquoted int breaks `tunnel route dns`).
- Service installed + enabled (runs as root from /etc/cloudflared). 4 edge conns (dar04/jnb04, QUIC).
- Verified: /api/v1/health → 401 (auth gate reachable), /identity → ready:true, runtime matches.
- NOTE: `cloudflared service install` needs config at /etc/cloudflared FIRST (it reads root's paths).
