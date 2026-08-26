# Sali — engineering guide

Sali is Almir's local-first personal AI agent. The Ollama model (`sali:latest`) is *only* the
reasoning engine; Sali is the deterministic system around it — memory, a temporal knowledge
graph, hybrid retrieval, a budgeted context engine, verified tools, a permission envelope, and
a journaled agent loop — over a single PostgreSQL 18 datastore. Full spec: `sali.txt` +
`sali2.txt`.

## Golden rules (from the spec's §46; enforced in code + tests)
- Never guess when the system can inspect the truth; never treat an LLM response as ground truth.
- Every memory carries **provenance + confidence + freshness**. Weak inference never becomes
  fact without evidence. Never silently overwrite contradictory history — record a contradiction
  and resolve by **evidence priority** (`source_priority`).
- Working memory can never become permanent silently (structural: `observe()` vs `remember()`).
- Verify every tool effect (plan → execute → observe → **verify**); never assume success.
- Model-agnostic: nothing above `provider/` imports a concrete backend (enforced by import-linter).
- Say when stale/uncertain; call a tool for live state. Protect secrets (redact at every boundary).

## Layering (enforced: `make lint` runs import-linter)
`cli → runtime → context → retrieval → (memory | graph) → (security | verify) → tools →
provider → db → obs → config → core`. Higher imports lower, never the reverse. `kernel.py` is
the single composition root (unlayered) that wires the object graph.

## Datastore
One database `sali` (+ `sali_test`), schema `sali`, forward-only numbered migrations in
`db/migrations/`. Peer auth over the unix socket — **no DB password**. Temporal validity is the
open interval `[valid_from, valid_until)`; `valid_until IS NULL` = current. The append-only
`event` log is Sali's durable action history (distinct from the per-run `run_events` journal).

## Dev workflow
```
make install                        # venv + deps
sudo bash scripts/bootstrap_db.sh   # one-time: role almir, dbs, extensions (needs your password)
make init                           # apply migrations + seed identity
make ci                             # ruff + import-linter + mypy --strict + pytest w/ 85% coverage gate
```
`make ci` is the gate. DB-backed tests use `sali_test` with rollback-per-test (or a truncating
`live_pool` fixture) and auto-skip if Postgres is unreachable. No test hits Ollama
(`FakeModelProvider`). Tests marked `@pytest.mark.live` are opt-in and excluded from CI.

## Conventions
- Async throughout; asyncpg. Enum columns pass `.value` with an explicit `::type` cast.
- JSONB round-trips as dicts (codec registered on every pooled connection).
- Functions that write memory/graph take a `conn` (caller owns the transaction); services wrap a pool.
- `mypy --strict` clean; `asyncpg`/`ollama` are `ignore_missing_imports` (typed as `Any`).
- New capability? Add a `Tool` (declare `risk_level`), register it, and let the policy gate it.
