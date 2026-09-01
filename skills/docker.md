---
name: Docker
tags:
  - docker
  - docker-compose
  - container
  - dockerfile
---

# Docker

## Basics
- Build: `docker build -t <name> .` from the workspace (the `Dockerfile` and context live in the workspace).
- Run: `docker run --rm -p <host>:<container> <name>`.
- Compose: `docker compose up -d` / `docker compose logs -f` / `docker compose down`.

## Writing a Dockerfile
- Pin a base image tag; order layers cheap→expensive (copy dependency manifests and install BEFORE copying the whole source, so the dependency layer caches).
- Use `.dockerignore` to keep the build context small.
- Prefer multi-stage builds for compiled/bundled apps.

## Verification
- The image builds (`docker build` exits 0) — that is the evidence, not "the Dockerfile looks right".
- The container starts and the health/endpoint responds (`docker ps`, `curl` the mapped port).

## Gotchas
- Port already in use → pick another host port.
- Build fails on a dependency step → the error names the missing package; fix and rebuild.
- Never run destructive `docker system prune -a` unless explicitly asked.
