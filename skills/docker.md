---
name: Docker
tags: [docker, container, dockerfile, compose, containerization]
dependencies: []
version_hint: docker@24+
project_detect:
  - Dockerfile
  - docker-compose.yml
  - compose.yml
  - .dockerignore
summary: Containers are reproducible units of deployment, not VMs. Multi-stage builds keep runtime images small. Never run as root. Cache layers by ordering commands from least-to-most-frequently-changing. Volumes for persistent data, never in the container's writable layer. Compose for local dev + small deployments; Kubernetes only when you actually need orchestration. Diagnose container problems by inspecting logs, exec-ing in, and checking the SPECIFIC layer that broke — never blindly rebuild.
---

# Docker (production)

## Dockerfile essentials

    # ── build stage ────────────────────────────────────────
    FROM python:3.14-slim AS builder
    WORKDIR /app
    COPY pyproject.toml uv.lock ./
    RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev
    COPY src ./src

    # ── runtime stage ──────────────────────────────────────
    FROM python:3.14-slim
    WORKDIR /app
    RUN useradd -m app && chown app:app /app
    USER app
    COPY --from=builder --chown=app /app /app
    ENV PATH="/app/.venv/bin:$PATH"
    EXPOSE 8080
    HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
        CMD curl -f http://localhost:8080/healthz || exit 1
    CMD ["python", "-m", "myapp"]

Key points:

* **Multi-stage**: build tools stay in the builder image; runtime image is minimal.
* **Base image**: `slim` variant (Debian slim) is the default sane choice. `alpine` is smaller
  but has musl+glibc gotchas (Python packages with C extensions can be broken).
* **User**: `USER app` — never run as root in production. Reduces blast radius.
* **Layer caching**: copy dependency files FIRST, install deps, THEN copy source. Source
  changes don't invalidate the deps layer.
* **HEALTHCHECK**: the container reports its own health to Docker.
* **EXPOSE**: documentation; doesn't publish the port. Publish with `-p` at run time.

## `.dockerignore`

    .git
    .venv
    node_modules
    __pycache__
    *.log
    .env*
    coverage/

* Every path listed is EXCLUDED from the build context. Faster builds + no secrets baked in.
* At minimum: `.git`, `.env*`, `node_modules`, `.venv`, `__pycache__`.

## Compose

    services:
      web:
        build: .
        ports: ["8080:8080"]
        depends_on:
          db: { condition: service_healthy }
        environment:
          DATABASE_URL: postgres://app:${DB_PASSWORD}@db:5432/app
        restart: unless-stopped
      db:
        image: postgres:17-alpine
        volumes: ["dbdata:/var/lib/postgresql/data"]
        environment:
          POSTGRES_USER: app
          POSTGRES_PASSWORD: ${DB_PASSWORD}
          POSTGRES_DB: app
        healthcheck:
          test: ["CMD-SHELL", "pg_isready -U app"]
          interval: 5s
          retries: 10
    volumes:
      dbdata:

* `depends_on: { condition: service_healthy }` waits for the healthcheck — plain
  `depends_on: [db]` only waits for the process to START, not for it to be READY.
* Named volumes for data — `dbdata` survives `docker compose down` (`down -v` wipes them).
* Secrets via env vars from a `.env` file (git-ignored) OR Docker secrets in prod. NEVER
  hardcoded.

## Volumes vs bind mounts

* **Volume**: `dbdata:/var/lib/postgresql/data` — Docker-managed; portable; better perf on
  Docker Desktop (macOS / Windows).
* **Bind mount**: `./src:/app/src` — live host directory. Great for dev (edit locally, run in
  container). Slow on Docker Desktop for large trees.
* Never store persistent app data in the container's writable layer. Container is disposable.

## Networking

* Compose networks are BRIDGES; services address each other by SERVICE NAME
  (`postgres://db:5432/`, not `postgres://localhost:5432/`).
* Publish ports only for services the OUTSIDE needs (`web`, sometimes `db` in dev). Internal
  services (redis, memcached) stay off the host.

## Secrets

* Never `ENV DB_PASSWORD=hardcoded` in a Dockerfile — bakes into the image layer, visible in
  `docker history`.
* Never `COPY .env .env` — same problem.
* At runtime: `docker run --env-file .env` OR Docker secrets (`/run/secrets/db_password`).

## Image size

Small = fewer CVEs + faster deploys.

* Multi-stage (above).
* Alpine WHERE it works (careful with Python C-extensions).
* `apt-get clean && rm -rf /var/lib/apt/lists/*` after `apt-get install`.
* Combine RUN steps to fold into one layer where sensible.
* `docker images | grep app` — target under 200 MB for most Python / Node apps; under 50 MB for
  Go / Rust binaries.

## Debugging

* `docker compose logs -f web` — stream logs.
* `docker compose exec web sh` — shell in a running container.
* `docker inspect <container>` — full state; look at Mounts / Networks / State.
* `docker events` — real-time event stream.
* `docker system df` — disk usage; prune with `docker system prune -a --volumes` (careful).

## Common problems + fixes

* **"file not found"** during build: `.dockerignore` excluded it, or the COPY path is wrong
  relative to the build context.
* **"connection refused"** to service `db`: `db` isn't ready; add a healthcheck.
* **Container exits immediately**: your CMD is a one-shot (returned). Something like
  `bash -c "sleep infinity"` is a lazy way to keep it up while debugging.
* **Permission denied writing volume**: UID mismatch. Container runs as UID 1000; volume owned
  by UID 0 on host. Fix with `chown` in the Dockerfile OR set UID at run time.
* **Slow bind mount on macOS**: use volumes for `node_modules` and `.venv`; bind mount just
  source code.

## Anti-patterns

* Running as root.
* `COPY . .` before `RUN pip install` (destroys layer cache).
* Storing state in the container writable layer.
* `latest` tag in production (`postgres:17` yes; `postgres:latest` no).
* One giant container running everything — split into services.
* `docker exec` for routine operations (should be an entrypoint script).
* Ignoring healthchecks.

## When NOT Docker

* Simple static site: use a CDN.
* One Python script: `pipx install` is fine.
* Anything requiring GPU + specific drivers: containers add friction; nvidia-docker helps but
  isn't free.
