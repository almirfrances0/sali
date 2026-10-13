---
name: PostgreSQL
tags: [postgres, postgresql, database, sql, pgvector]
dependencies: []
version_hint: postgres@16 or 17
project_detect:
  - docker-compose.yml
  - schema.sql
  - migrations/
summary: Postgres is the default choice for a serious app. Model normalised (3NF as a starting point); denormalise only with evidence. Every table has a PK; every FK has an index; every unique constraint is explicit. Parametrise every query (never string-interpolate). Transactions for related writes; migrations forward-only + reversible. `EXPLAIN ANALYZE` before optimising. Backups nightly + tested restore drill quarterly. Extensions worth knowing — pgvector, pg_trgm, uuid-ossp / gen_random_uuid.
---

# PostgreSQL (production)

## Schema design

    CREATE TABLE users (
        id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        email         TEXT UNIQUE NOT NULL,
        name          TEXT NOT NULL,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE orders (
        id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        status        TEXT NOT NULL CHECK (status IN ('open', 'paid', 'shipped', 'cancelled')),
        total_cents   BIGINT NOT NULL CHECK (total_cents >= 0),
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE INDEX ix_orders_user_status ON orders(user_id, status);
    CREATE INDEX ix_orders_created ON orders(created_at DESC);

* Every table has a PK. UUID (v4 via `gen_random_uuid()`) is default; bigserial for high-volume
  logs where sequence matters.
* TIMESTAMPTZ, never TIMESTAMP — the tz-naive one is a footgun.
* NOT NULL by default; nullability is opt-in.
* CHECK constraints for enum-like columns until you need a real enum type.
* FK ON DELETE CASCADE / RESTRICT / SET NULL — pick per business rule.

## Indexes

* Every FK column should be indexed (Postgres doesn't auto-index FKs).
* Every WHERE / JOIN column should have an index if queries take > 100 ms.
* Composite indexes read left-to-right: `(user_id, status)` serves `WHERE user_id = ?` and
  `WHERE user_id = ? AND status = ?`, but NOT `WHERE status = ?` alone.
* Partial indexes: `CREATE INDEX ix_orders_open ON orders(created_at) WHERE status = 'open'` —
  index only the rows you actually query.
* `EXPLAIN (ANALYZE, BUFFERS)` on slow queries — look for Seq Scan on big tables, wrong join
  order, missing indexes.

## Transactions

    BEGIN;
    UPDATE accounts SET balance = balance - 100 WHERE id = $1;
    UPDATE accounts SET balance = balance + 100 WHERE id = $2;
    INSERT INTO transfers (from_id, to_id, amount) VALUES ($1, $2, 100);
    COMMIT;

* Related writes go in a transaction. If any fails, ROLLBACK.
* Serialisable isolation for money / accounting; READ COMMITTED (default) for everything else.
* NEVER hold a transaction open for a long-running operation (external HTTP call, etc.) —
  locks pile up.
* `SELECT ... FOR UPDATE` to lock rows for the duration of the transaction (row-level lock).

## Migrations

* Forward-only in production. Each migration has an `up.sql` and MUST have a `down.sql` that
  reverses it (even if you never run `down` in prod, writing it forces you to think).
* Additive first: add a nullable column, backfill in a separate migration, then add NOT NULL.
  This is the ONLY safe way for a large table under load.
* Never `DROP COLUMN` in the same migration that STOPS using it — deploy the code first, then
  a later migration drops it. Otherwise the old code runs against the new schema during rollout.
* Migration tools: Alembic (Python), Flyway, sqitch. Roll-your-own is fine for a small project;
  make sure it runs migrations in order + skips already-applied ones idempotently.

## Query patterns

* SELECT what you need — never `SELECT *` in production code.
* Parametrised queries always: `$1`, `$2` (asyncpg), `%s` (psycopg), `?` (sqlite). NEVER
  f-string interpolation.
* JOIN in the DB, not in the app — pulling all users + a Python `for u in users: fetch_orders(u)`
  is the N+1 antipattern.
* `LIMIT` on user-facing queries always. `SELECT * FROM logs` in a UI = disaster.
* Aggregates: `COUNT(*)` is fast on Postgres if the visibility map is up to date; `COUNT(col)`
  where `col` might be NULL is slower.

## Connection pooling

* Never open a connection per request from the app — pool.
* asyncpg: `asyncpg.create_pool(min_size=5, max_size=20)` at app startup.
* PgBouncer between app and Postgres for connection-heavy workloads (multiplex thousands of
  app connections into tens of Postgres connections).
* Postgres default `max_connections=100`; go past it = errors. Pool sizing must account for
  every worker.

## Backups

* `pg_dump -Fc` (custom format) nightly. Compressed, parallel-restorable.
* Store off-machine (S3, cloud storage). Encrypt at rest.
* **TEST RESTORE quarterly.** An untested backup is not a backup — see the sali-scratch-db-testing
  memory for the classic "we've been backing up for 3 years, restore fails" story.
* For zero-downtime PITR: WAL archiving + base backup + pgbackrest. Beyond ~50 GB of data,
  worth the setup.

## Extensions worth knowing

* `pgcrypto` / `gen_random_uuid()` — UUIDs in DB (default in Postgres 13+ under core).
* `pg_trgm` — trigram similarity for fuzzy text search + `LIKE '%foo%'` acceleration.
* `pgvector` — vector similarity search for embeddings; supports IVFFlat and HNSW indexes.
* `unaccent` — accent-insensitive search.
* `pg_stat_statements` — track most-frequent + slowest queries. Enable in prod always.

## Anti-patterns

* String-concat SQL — injection risk + no plan caching.
* Ignoring `EXPLAIN` — optimising by guessing.
* `SELECT *` in production code.
* Missing FK indexes.
* `TIMESTAMP` (naive) instead of `TIMESTAMPTZ`.
* Storing enum values as `varchar(255)` — use `text` (same performance) with a CHECK, or a
  real ENUM type.
* Editing a migration that's been deployed.
* `DELETE` without `WHERE`.
* Connecting as `postgres` (superuser) from the app.
* No backups. Or "backups" without a restore drill.

## Security

* App connects as a role that CANNOT `DROP TABLE`. Separate migration role for DDL.
* Password auth over plain TCP → `SSL required`. Set `sslmode=require` on the connection string.
* Row-level security (RLS) for multi-tenant: `CREATE POLICY tenant_isolation ON orders USING
  (tenant_id = current_setting('app.tenant_id')::uuid)`. Every query is auto-filtered.
* Never expose PostgREST / Hasura / equivalent to the Internet without another auth layer.
