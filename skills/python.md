---
name: Python
tags: [python, py, pytest, asyncio, typing, dataclass]
dependencies: []
version_hint: python@3.12+
project_detect:
  - pyproject.toml
  - setup.py
  - setup.cfg
  - requirements.txt
summary: Modern Python (3.12+) is typed, async-aware, and packaged with pyproject.toml. Type hints on every public function; `from __future__ import annotations` on top of every file; `dataclass(slots=True)` for value types; asyncio only for I/O-bound; ruff + mypy + pytest as the toolchain; venv-per-project; never `pip install --user`. Async and sync must not mix in the same call chain without `run_in_threadpool`.
---

# Python (production)

## Toolchain

* Package manager: pip + venv (baseline), poetry or uv (modern; uv is faster).
* Linter/formatter: **ruff** (replaces flake8, black, isort, pylint for 90% of use cases).
* Type checker: **mypy** (or pyright). Strict mode on new code. `Any` requires a comment.
* Test runner: **pytest** + pytest-asyncio.
* Dependency file: **pyproject.toml** (PEP 621) — never `requirements.txt` for a real project.

## Type hints

    from __future__ import annotations
    from dataclasses import dataclass, field
    from typing import Protocol
    from collections.abc import Iterator, AsyncIterator

    @dataclass(slots=True, frozen=True)
    class User:
        id: str
        name: str
        email: str | None = None

    class Repo(Protocol):
        async def get(self, id: str) -> User | None: ...

* `from __future__ import annotations` — top of every file. Makes types lazily evaluated
  (`User` doesn't need to exist at import time when used in an annotation).
* `X | Y` is the union syntax (3.10+). `Optional[X]` is old. `list[X]` not `List[X]`.
* `Protocol` for duck-typed contracts; ABCs only when inheritance is truly needed.
* `@dataclass(slots=True)` — smaller memory, no `__dict__`, faster attribute access.
  `frozen=True` for value types.
* Return `list[X]` for immutable-facing APIs, or return an iterator. Never return a generator
  the caller can only iterate once and calls it a list.

## asyncio

* Async is for I/O-bound work — thousands of concurrent DB queries, HTTP fetches, WS messages.
* Sync is for CPU-bound work — image processing, ML inference, heavy math.
* NEVER mix: a `time.sleep(1)` inside an `async def` blocks the ENTIRE event loop for 1 s. Use
  `await asyncio.sleep(1)` (or `run_in_threadpool` for genuinely sync deps).
* `asyncio.gather(*tasks)` for concurrency; `asyncio.TaskGroup` (3.11+) for structured
  cancellation.
* Timeouts: `async with asyncio.timeout(5.0): await work()`. Never `asyncio.wait_for` in new
  code (timeouts are less composable).
* Cancellation: any task must be cancellable. `try: ... except asyncio.CancelledError: raise`.
* `asyncio.Semaphore(N)` to cap concurrent I/O when the downstream isn't limitless.

## Data classes vs pydantic vs attrs

* Value types with no validation: `@dataclass(slots=True, frozen=True)`.
* Value types with runtime validation (request/response shapes, config): Pydantic v2.
* Legacy code with attrs: keep it. Don't churn.
* NEVER regular dicts for domain objects — untyped, unmaintainable.

## Context managers + generators

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def db_transaction(pool) -> AsyncIterator[Connection]:
        async with pool.acquire() as conn, conn.transaction():
            yield conn

* One resource acquisition per `with`; nesting is fine.
* `try/finally` inside a generator: use `contextlib.contextmanager` — the sugar handles cleanup
  even on exceptions.

## Exceptions

* Raise a SPECIFIC type; catch specific types; re-raise if you can't handle.
* `except Exception:` is a smell — you're saying "I don't know what could go wrong here". If
  you must (top-level supervisor, logging boundary), narrow with `# noqa: BLE001` and a comment.
* Never `except: pass` — silent-swallow is how bugs live for years.

## Packaging

    [project]
    name = "myapp"
    version = "1.0.0"
    requires-python = ">=3.12"
    dependencies = ["fastapi>=0.115", "asyncpg>=0.30"]

    [project.optional-dependencies]
    dev = ["pytest>=8", "ruff>=0.7", "mypy>=1.13"]

    [project.scripts]
    myapp = "myapp.__main__:main"

* Version pinning: lower-bound on runtime deps (`>=`), upper-bound for known incompatibilities.
  Lockfile for reproducibility.
* `__main__.py` for CLI entry: `python -m myapp` works.
* `src/` layout — put code under `src/myapp/`; tests under `tests/`. Prevents accidental
  imports from the workspace root.

## venv per project

* Always. Never `pip install` into system Python (fedora / debian / mac /whatever).
* `python -m venv .venv` — the stdlib venv is fine. `uv venv` is faster.
* `.venv/` in `.gitignore`.

## Testing

    # tests/test_users.py
    import pytest
    from myapp.users import get_user

    @pytest.mark.asyncio
    async def test_get_user_returns_none_when_missing(pool):
        assert await get_user(pool, "nope") is None

* Fixtures over inline setup. `conftest.py` for shared fixtures (pool, factory functions,
  fake clients).
* pytest-asyncio in `auto` mode (`asyncio_mode = "auto"` in pyproject.toml) so `async def
  test_*` doesn't need `@pytest.mark.asyncio` decorator every time.
* Fast: unit tests << 100 ms each. If a unit test needs a DB, it's an integration test — mark
  it accordingly.

## Logging

    import structlog
    log = structlog.get_logger()
    log.info("user_created", user_id=uid, source="api")

* `structlog` for structured JSON logs. Never `print()` in library code.
* Never log passwords / tokens / PII. See the security skill.
* Log the STRUCTURED FACTS (user_id, action, status), not sentences ("User john logged in from…").

## Common performance traps

* Building a list from a generator when you needed an iterator: `list(map(f, items))` when
  you could `for r in map(f, items): ...`.
* `.append()` in a loop where a comprehension is clearer.
* Repeated attribute lookup in a hot loop: `for x in items: obj.method(x)` — cache
  `method = obj.method` before the loop.
* `+= str` in a loop — `"".join(parts)`.
* `import` inside a hot function — imports run once at module load; put them at the top.
* Regex compiled at function-call time — module-level `_RE = re.compile(...)`.

## Anti-patterns

* Mutable default arguments: `def f(x=[]):` — the list is SHARED across calls.
* `except Exception:` without re-raise — silent failure.
* `os.system(user_input)` — shell injection.
* Global mutable state — thread-safety, test isolation both broken.
* Circular imports — a design smell. Extract the shared thing to a third module.
* `from module import *` — unclear origins; namespace pollution.
