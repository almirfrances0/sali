---
name: Testing & Quality
tags: [testing, quality, tests, unit, integration, e2e, pytest, ci]
dependencies: []
summary: "It runs" ≠ "it works". Tests are executable documentation of the contract. Prefer real integrations over mocks where feasible (mocked-behaviour ≠ real-behaviour). Unit tests catch logic; integration catches wiring; e2e catches the seams. Coverage % is a smell (100% covered wrong code still fails). Golden path + edge cases + adversarial verification. Static analysis + type checks + linting run first (cheapest to fix).
project_detect:
  - pytest.ini
  - pyproject.toml
  - vitest.config.ts
  - jest.config.js
  - phpunit.xml
---

# Testing & Quality

## The layers

* **Static analysis / type checks / linting** — run FIRST. Cheapest failures to fix. `mypy`,
  `tsc --noEmit`, `phpstan`, `ruff`, `eslint`. Zero warnings in production code.
* **Unit tests** — one function or class in isolation. Fast (< 1 ms per test), pure, no I/O.
  Cover the logic the compiler / type-checker doesn't. Property-based (Hypothesis, fast-check)
  where the domain is amenable.
* **Integration tests** — real DB, real files, real subprocesses. What the module actually does
  when wired to its collaborators. Slower (10 ms – 1 s). Uses a scratch DB (see the
  sali-scratch-db-testing memory).
* **API / contract tests** — HTTP in, HTTP out. FastAPI's `TestClient` or Laravel's
  `$this->getJson()`. Runs the app in-process against a scratch DB.
* **E2E / browser tests** — full stack with a real browser (Playwright, Cypress). Slow. Cover
  the two or three flows a user does daily; NOT every branch.
* **Performance tests** — for latency-critical paths (chat first-token, WS delivery). Measure,
  don't guess.
* **Security tests** — auth boundaries, permission escalation, input fuzzing on high-value
  endpoints.
* **Accessibility tests** — `axe-core` in CI on representative pages.

## Mocks — when and when NOT

Mocks are useful when the REAL dependency is genuinely unavailable or too slow.

Mocks are ACTIVELY HARMFUL when they let a passing test hide a broken integration:

* Mocking a database in a test that's supposed to prove the SQL works: the SQL is not tested.
  See the sali-scratch-db-testing memory — "we got burned last quarter when mocked tests passed
  but the prod migration failed".
* Mocking an HTTP client in a test that proves your parsing works against a real API response
  shape: your test proves nothing about the real API.
* Mocking `time.time()` to make a flaky test pass: the test is not flaky; the code is racy.

Prefer:
* Real DB (scratch schema, transactions rolled back per test).
* Real HTTP against a WireMock / VCR fixture recorded from the real server.
* Real subprocess against a small canned binary.

Mock the LAST thing you can — the external LLM, the payment provider, APNs. Everything else is
usually reachable in-process.

## Adversarial verification

Every "it works" claim gets a "prove it's wrong" pass:

* Wrong input: what does the endpoint do with a null? An empty string? An enormous string?
  A field with the wrong type?
* Wrong state: what if the DB row doesn't exist? What if it exists but is revoked / expired /
  deleted?
* Wrong order: two requests hit the endpoint concurrently — what happens?
* Wrong network: the response body is truncated / the connection drops mid-request.
* Wrong time: a token created NOW; asked about at NOW + 1 hour; at NOW - 1 hour.

Write tests for each failure mode you can think of. Then ask someone else (or the adversarial-
review pattern) to think of ones you missed.

## The trap of coverage %

100% line coverage on wrong code still ships wrong code. Coverage tells you what was RUN, not
what was VERIFIED. A line covered by a test with no assertion is not tested — it's exercised.

Better signals:
* Every public function has at least one test that asserts on its return value.
* Every code path with `if` / `else` has a test for each branch.
* Every reported bug has a regression test.
* Coverage > 80% for critical paths (auth, payments, permission checks). For internal utilities,
  cover the CONTRACT (what callers rely on).

## Regression tests

When you fix a bug: add the test that would have caught it. This is the ONLY way the same bug
doesn't come back six months later during a refactor.

The test's docstring should say WHAT BUG: "test_refresh_grace_reissues_after_lost_response —
prod complaint 'requires re-enrollment on restart' recurs when refresh_session rejects an
already-rotated token; grace path fixes."

## Fixtures + factories

* Fixtures over inline setup — one authoritative way to create each domain object.
* Factories (factory-boy, model_bakery) for domain objects with sensible defaults + explicit
  overrides.
* Never share state between tests. Each test creates what it needs; the runner isolates.

## Test data

* Deterministic — no randomness, no wall-clock, no environment. If you need "random" IDs use
  `uuid4()` but never assert on the value; assert on the SHAPE.
* Small — one row, two rows, ten rows. NEVER thousands, unless you're testing scale
  specifically (and that's a separate perf test).
* Realistic — a test order has a real price, a real product, a real user; not `Order(1)`.

## CI

* Linting + type-check + unit tests on every PR. Should finish in < 2 min for the whole repo.
* Integration + API tests on every merge to main. Should finish in < 15 min.
* E2E + perf on nightly. Slow, expensive, can afford to run once per day.
* Fail the build on ANY test failure. No "flaky test" merges. If a test is flaky, quarantine it
  (mark `@pytest.mark.flaky` + open a bug) or delete it — never leave it failing.

## Production readiness — the real checklist

Before shipping, verify:

1. Happy path works end-to-end.
2. Every reported bug has a regression test.
3. Static analysis: zero warnings.
4. Type check: zero errors.
5. Test suite: 100% pass. Not 99%. 100.
6. Error paths tested: what does the endpoint do at auth failure? DB down? Rate limit hit?
7. Load: N concurrent users on the target hardware — latency + error rate within SLO.
8. Security: no known CVEs in dependencies; auth boundaries verified.
9. Migrations: apply forward AND back on a scratch DB with production-shape data.
10. Observability: logs meaningful; metrics exported; dashboards updated.
11. Rollback plan documented.

"It works on my machine" is not shipping. "The tests pass" is not shipping. "It's been in prod
for a week without complaints" starts to look like shipping.
