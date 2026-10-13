# Sali

Sali is a local-first, long-lived personal AI agent that lives on one machine — a persistent
digital person with durable identity, memory, perception, initiative, and the ability to learn
from experience over time.

He is not a chatbot and not a coding agent. He is one living process with one reasoning path,
designed around a single invariant:

> **ONE PC · ONE SALI · ONE MIND**

Identity, memory, goals, and experience live in PostgreSQL — not in model weights or prompt
strings. The reasoning model is a replaceable substrate behind a provider abstraction.

## What is in this repository

- `src/sali/` — the agent runtime (kernel, agent loop, memory, graph, retrieval, learning,
  perception, tasks, tools, security, API, CLI)
- `ios/` — the iPhone app (built on a Mac with Xcode; see `ios/docs/`)
- `systemd/` — host units: Sali daemon, power envelope, VRAM guard, nightly backup
- `scripts/` — host setup, power envelope, backups, restore drill
- `docs/` — architecture and engineering notes
- `tests/` — the test suite (`make ci` runs lint + types + coverage)

## Quick start

See [INSTALL.md](INSTALL.md) for the full installation guide. In short:

```bash
make install          # venv + editable install
.venv/bin/sali init   # apply every migration, seed identity
.venv/bin/sali doctor # Postgres reachable? migrations current? model server up?
.venv/bin/sali daemon # become THE Sali on this machine
```

## Development

```bash
make ci    # ruff + import-linter + mypy --strict + pytest with coverage
make test  # fast test pass (excludes live model tests)
```
