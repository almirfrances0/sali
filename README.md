# Sali

A local-first, long-lived personal AI agent that lives on Almir's Kali desktop. The Ollama model
(`sali:latest`) is *only* the reasoning engine; Sali is the surrounding system — multi-layer memory,
a temporal knowledge graph, deterministic sensing, evidence-gated learning, and a verified tool +
security envelope, all over a single PostgreSQL 18 datastore.

The full architecture is in [`sali.txt`](sali.txt) + [`sali2.txt`](sali2.txt) and the published
A–P proposal.

## Phase 0 — skeleton & spine (this milestone)

```bash
make install         # create .venv and install Sali + dev tools
sudo bash scripts/bootstrap_db.sh   # one-time: create role `almir`, db `sali`/`sali_test`, pgvector
make init            # apply migrations to the `sali` database
make doctor          # check Postgres + Ollama + migration state
make ci              # ruff + mypy + pytest (DB tests auto-skip if Postgres is unreachable)
```

### Layout

```
src/sali/
  core/      ids, clock, enums, errors            # shared vocabulary
  config/    settings.py                          # layered pydantic-settings
  obs/       log.py                               # structured logging
  db/        pool, uow, migrations/(runner+0001)  # one datastore, forward-only migrations
  provider/  base (Protocol) + presets + fake + ollama + registry   # model-agnostic
  cli/       main.py                              # `sali init | doctor | chat | version`
  kernel.py                                       # single composition root
```

Nothing is persisted as fact until Phase 2. See the Makefile for the developer workflow.
