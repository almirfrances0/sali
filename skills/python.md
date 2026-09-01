---
name: Python
tags:
  - python
  - pip
  - venv
  - pytest
  - poetry
---

# Python

## Environment
- Use a virtual environment inside the workspace: `python3 -m venv .venv && . .venv/bin/activate`.
- Install deps: `pip install -r requirements.txt` (or `pip install -e .` / `poetry install`).
- Pin versions; regenerate las needed with `pip freeze > requirements.txt`.

## Structure
- Package code under `src/<pkg>/`; tests under `tests/`.
- Prefer explicit imports and type hints.

## Verification
- Tests: `pytest -q`. A green run is the evidence.
- Types/lint if configured: `mypy`, `ruff check`.
- Import the module or run the entry point to confirm it actually works — not just that the file exists.

## Gotchas
- Don't install into the system Python; always the workspace venv.
- A failing import after install usually means a missing sub-dependency or the wrong venv activated.
