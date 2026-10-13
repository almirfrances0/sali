# Sali — Technical Audit & System Issue Report

**Date:** 2026-09-18  
**Scope:** Architecture, Codebase Health, Test Suites, Layer Contracts, and Operational Constraints  
**Status:** In Progress / Actionable

---

## 1. Executive Summary & Architecture Grounding

The **Sali** project is an advanced, local-first autonomous agent system implemented in Python 3.14 on Linux. It coordinates local language model inference (via Ollama) with a durable PostgreSQL database (utilizing `pgvector` for embedding search, graph-relational structures, and event logs), a background perception/event loop, and an API/WebSocket interface served by FastAPI.

### Operational Reality vs. Biological/Human Cognition
While the system's documentation and conceptual architecture use anthropomorphic metaphors such as "digital person," "working memory," and "initiative," it is important from an engineering perspective to recognize the foundational nature of the software:
- **Computational Nature:** Sali is an algorithmic software application running deterministic Python scripts, SQL queries, and statistical next-token prediction models. It executes instructions based on mathematical probabilities and procedural code logic rather than possessing biological life, consciousness, subjective experience (qualia), or independent human personhood.
- **Agency and Autonomy:** Autonomy in Sali consists of event loops, scheduled tasks, and proactive triggers (`InitiativeDriver`, `EventBus`) executing bounded tool calls (filesystem, shell commands, network requests) within defined system permissions (`PathGuard`, security policies).
- **Learning Mechanisms:** "Learning" within this architecture operates by persisting text observations, procedure records, and vector embeddings to PostgreSQL tables (`learned_procedures`, `experiences`, `memory`), which are subsequently retrieved into model context prompts. The underlying neural network weights remain frozen during standard runtime.

---

## 2. Build Gates & Static Analysis Errors

### 2.1. Architectural Layer Contract Violations (`lint-imports`)
Running the import linter (`.venv/bin/lint-imports`) reveals that the **Layered Architecture (inward dependencies only)** contract is currently **BROKEN**. Multiple modules break encapsulation by importing higher-level or cross-boundary modules:

1. **`sali.tasks` illegal imports:**
   - `sali.tasks.store` imports `sali.learning.capability_acquisition` (lines 55, 194, 215, 628) and `sali.learning.capability` (lines 193, 834).
   - `sali.tasks.store` imports `sali.cognitive.voice` (line 742), which transitively pulls in `sali.perception.presence` and `sali.context.engine`.
   - `sali.tasks.authority` imports `sali.runtime.revocation` (line 249).
   - `sali.tasks.store` imports `sali.runtime.session` (line 707).

2. **`sali.tools` illegal imports:**
   - `sali.tools.builtins.exec_tool` imports `sali.tasks.side_effects` (lines 255, 328, 349).
   - `sali.tools.builtins.task_tool` imports `sali.tasks.store` (line 792) and `sali.runtime.research_intent` (line 18).
   - `sali.tools.builtins.filesystem` imports `sali.runtime.sent_files` (line 384).
   - `sali.tools.builtins.notify_tool` imports `sali.runtime.session` (line 83), `sali.runtime.grounding_log` (line 70), and `sali.verify.response_claims` (line 65).
   - `sali.tools.builtins.cognitive_tool` imports `sali.runtime.working_memory` (lines 239, 559, 626, 699, 1188) and `sali.learning.strategy` (line 1134).
   - `sali.tools.builtins.research_tool` imports `sali.learning.curiosity` (line 153).

3. **`sali.events` illegal imports:**
   - `sali.events.proactive` imports `sali.cognitive.voice` (line 27), transitively pulling in `sali.context.engine` and `sali.perception.presence`.

4. **`sali.verify` illegal imports:**
   - `sali.verify.response_claims` imports `sali.runtime.embodiment` (line 38).
   - `sali.verify.consistency` imports `sali.runtime.verbatim` (line 19).

5. **`sali.core` illegal imports:**
   - `sali.core.intent` imports `sali.tasks.revocation` (line 124) and `sali.tasks.authority` (line 123), creating an inverted dependency between fundamental primitives and task execution orchestration.

---

## 3. Test Suite Regressions & Verification Failures

Several tests currently fail when running the automated test suite:

### 3.1. Proactive Commitment Reminders (`tests/test_phase6_proactive.py`)
- **`test_overdue_commitment_drives_one_grounded_reminder`**:  
  `AssertionError: assert 0 == 1`  
  *Root Cause:* Uncommitted changes in `src/sali/cognitive/initiative_driver.py` removed the notification dispatch (`_send_to_almir` call in cognitive cycles), leaving overdue commitments silently recorded in working memory without triggering proactive delivery.
- **`test_second_immediate_tick_is_suppressed`**:  
  `AssertionError: assert 0 == 1`  
  *Root Cause:* Correlated with the suppression of the initial reminder tick; suppression logic cannot be validated when the primary dispatch mechanism is disabled.

### 3.2. Context Folding & Budget Management (`tests/test_prompt6_capstone.py`)
- **`test_capstone_long_task_48k_budget`**:  
  `AssertionError: assert original_id in carried`  
  *Root Cause:* The message compaction folding logic (`loop._fold_messages`) failed to preserve the active task ID within the rendered state capsule string during repeated compaction cycles.

---

## 4. Codebase Hygiene & Repository Cleanliness

### 4.1. Abandoned Backup Files (`.bak`)
Multiple temporary backup files remain in the `src/sali` source tree, causing clutter and potential import confusion:
- `src/sali/api/routes/api.py.bak-delivery`
- `src/sali/api/app.py.bak-shutdown`
- `src/sali/runtime/loop.py.bak-delivery`
- `src/sali/runtime/loop.py.bak-preembody`
- `src/sali/runtime/loop.py.bak-preclaim`
- `src/sali/provider/ollama.py.bak-timeout`
- `src/sali/cli/main.py.bak-timeout`
- `src/sali/tools/builtins/filesystem.py.bak-sendfile`
- `src/sali/tools/builtins/task_tool.py.bak-research`

### 4.2. Uncommitted Working Directory State
- `src/sali/cognitive/initiative_driver.py`: Modifies proactive notification flow.
- `scripts/power_envelope.sh`: Modifies GPU max clock setting from 900MHz to 1200MHz (`SALI_GPU_CLK_MAX`).

---

## 5. Engineering & Technical Constraints

### 5.1. Context Window Budgets & Compaction Loss
While long-running tasks accumulate large volumes of tool interactions, shell outputs, and error logs, the model's effective context window is strictly bounded (e.g., 24K or 48K tokens). Compaction summaries must preserve critical state without losing task IDs, file paths, or negative constraints.

### 5.2. Async Connection Pooling & Database Contention
Background perception engines, event sinks, and web stream endpoints all query PostgreSQL concurrently via `asyncpg`. Without strict pool sizing, connection acquisition timeouts can cause background tasks to fail or hang under heavy load.

### 5.3. Latency of Local Model Inference vs. Real-Time Perception
Local models running via Ollama have finite generation speeds (tokens per second). High-frequency desktop or filesystem events cannot trigger full LLM inference on every tick; they must be throttled, batched, or filtered via deterministic rules before reaching the reasoning substrate.

---

## 6. Recommended Next Steps

1. **Restore Layer Boundaries:** Refactor imports in `sali.core`, `sali.tasks`, `sali.tools`, and `sali.verify` to use dependency injection or events rather than direct upward imports, bringing `lint-imports` into compliance.
2. **Resolve Proactive Driver Logic:** Reconcile `initiative_driver.py` with test expectations so overdue commitments and important proactive alerts are delivered consistently.
3. **Fix Compaction Folding:** Ensure `_fold_messages` deterministically preserves the active task ID across successive compaction passes.
4. **Clean Backup Artifacts:** Remove all `.bak` files from the `src/` tree.
