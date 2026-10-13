---
name: Security
tags: [security, auth, authz, xss, csrf, sqli, ssrf, secrets, hardening]
dependencies: []
conflicts: []
version_hint: n/a
project_detect: []
summary: Security is a property of every layer, not a stage at the end. Auth (who) is different from authz (what). Secrets never in code or logs — use a vault. Every input is untrusted. Every rendering is contextual (HTML escape ≠ JS escape ≠ URL escape). Every dependency is a supply-chain risk. Every path is a traversal risk. Every free-text field is a payload. Rate-limit by identity, not IP. Log without leaking. Assume the network is hostile.
---

# Security (production posture)

## Authentication vs authorization

* **Authentication**: who is this? Answered by tokens, sessions, keys, mTLS.
* **Authorization**: what may they do? Answered by roles, policies, capabilities, ownership.

These are separate concerns. A caller with valid auth is not authorised to do anything specific
until you check.

## Tokens + sessions

* Short-lived ACCESS tokens (15 min typical) + longer REFRESH tokens (7-30 days). Rotate on use.
* Store server-side as HASHES (SHA-256), never plaintext.
* Refresh rotation with a GRACE WINDOW — a client whose response was lost in transit must be
  able to retry once with the OLD token (see Sali's own refresh_session for a real example).
  Without grace, users get re-enrollment prompts every network flap.
* Rotated tokens must retire prior successors on the SAME identity — otherwise a captured mid-
  air token can race a legitimate rotation.
* Never log the access token. Log the SESSION ID or a hash prefix.

## Password / secret storage

* Passwords: argon2id (preferred) or bcrypt cost 12+. NEVER md5, sha1, sha256, or raw
  "encryption".
* Secrets in an encrypted vault, keyed by service. Rotate on suspicion of compromise.
* Environment variables are a REASONABLE-CASE store (not a great one). Never `git commit` a
  `.env`. Add `.env*` to `.gitignore` and scan the repo history.
* Service-to-service secrets: mint scoped tokens or use OIDC / IAM roles where the platform
  supports it. Long-lived shared secrets are debt.

## Input handling

Every input is untrusted, even when it's from an authenticated user (users can be compromised).

* **Types**: parse into typed models (Pydantic, TypeScript-checked, Zod, dataclass). Reject
  malformed input at the boundary; the deeper into the stack a raw string travels, the more
  places it can be misinterpreted.
* **Bounds**: every string has a max length; every list has a max size; every number has a range.
  Enforce at the parser, not with runtime checks scattered through the code.
* **Enums**: a status field with 4 valid values is an enum in the schema, not `str`.
* **URLs / paths**: reject anything that isn't a canonical, absolute, allow-listed value.

## Output rendering

Escaping is CONTEXTUAL — HTML, JavaScript, URLs, shell, SQL each need different escaping.

* HTML: `{value}` in a JSX / Blade / Jinja template is HTML-escaped by default. Do NOT
  `{@html value}` / `{{!! $value !!}}` / `|safe` unless you have already sanitised.
* JavaScript embed: JSON-encode. Do not build JS strings by concatenation.
* URL: `encodeURIComponent()` for query params. Never string-concat user input into URLs.
* Shell: never `os.system(user_input)`. Use `subprocess.run([...], shell=False)` with an argv
  list. Escape only if you cannot avoid a shell string.
* SQL: parametrised queries always. Never `f"SELECT * FROM users WHERE id = {id}"`. asyncpg
  uses `$1`, psycopg uses `%s`, sqlite3 uses `?`; use the right placeholder.

## SQL injection

* Every query with a variable is parametrised. Every one. The linter will catch f-string SQL;
  code review must catch the rest.
* ORMs help but don't guarantee it — `Model.query.raw(f"...{user_input}...")` is still SQLi.
* Test: `admin'--` in a login form should NOT log in as admin, obviously; also, the app must
  not error 500 (which reveals the schema).

## XSS

* Reflected: user input in a query param renders unescaped in the page. Fix: escape output.
* Stored: user input in the DB renders unescaped when other users view it. Fix: escape output +
  sanitise HTML input (bleach / DOMPurify) if you MUST accept HTML.
* DOM-based: JS reads `location.hash` and writes it to `innerHTML`. Fix: never assign untrusted
  strings to `innerHTML` / `outerHTML`; use `textContent`.
* CSP: strict `Content-Security-Policy` header caps the blast radius. `script-src 'self'`
  (no inline, no eval) + `object-src 'none'` are the strong baseline.

## CSRF

* Session-cookie apps: CSRF token in a hidden form field + header on every state-changing
  request. Same-Site=Lax on session cookies mitigates most attacks; explicit CSRF tokens close
  the remaining gap.
* Bearer-token APIs (mobile, SPA with `Authorization: Bearer …`): NOT vulnerable to classic
  CSRF (the browser doesn't automatically attach the header). Do not confuse "no CSRF token" with
  "no auth" — both must be true.

## SSRF

* Server-side fetch of a user-supplied URL: the URL may be `http://169.254.169.254/…` (cloud
  metadata) or `http://localhost:5432/` (internal service). VALIDATE + BLOCKLIST private
  ranges + follow-redirect off (or manually cap).
* Prefer allow-listed hosts if the use case is bounded ("only these three domains").

## Command injection

* `subprocess.run([...], shell=False)` with an argv list. Never build a shell string from user
  input. If a legacy dependency requires a shell, `shlex.quote()` every user-supplied component.
* File uploads with a "run this" step (image thumbnailing, PDF rendering) — the file name / MIME
  may be attacker-controlled. Use a temp file with a randomly-generated name; call the tool with
  the full path.

## Path traversal

* User-controlled filenames must be sanitised: strip directory components, allow-list characters
  (`^[A-Za-z0-9._-]+$`), then constrain the resolved path to a workspace root.
* `os.path.realpath` for the resolve; verify `is_within(resolved, workspace_root)` (real-path on
  both sides) before opening. Prevents symlink escape too.

## File upload security

* Cap size at the middleware boundary (headers `content-length`) AND at the reader
  (progressive limit). A chunked-transfer request can bypass a naïve content-length check.
* Content-type sniffing (`libmagic`) on uploaded files — never trust the client-declared MIME.
* Store outside the web root, or with a randomly-generated filename, and serve through a
  dedicated download endpoint that checks auth.
* Never execute or `include` an uploaded file. Never save an upload with a `.php` / `.jsp` /
  `.aspx` extension into any directory the server executes.

## API security

* Bearer tokens over HTTPS only. On plain-text LAN (see LAN discovery), understand that the
  bearer is sniffable — trust the network segment or add transport auth (mTLS / cert pinning).
* Rate limit by identity (device / user id), NOT by IP — IPs move (mobile carriers, corporate
  NAT). Rate-limit-by-IP is broken security theatre.
* Response bodies: no stack traces, no internal error messages, no schema hints. A 500 says
  "internal server error"; the details go to your log.

## WebSocket security

* Authenticate at the handshake, before `accept()`.
* Close codes 4001 (unauthorized), 4029 (too many connections), 4004 (bad request) — distinct
  so clients can act.
* Message-level auth for high-value operations if the connection is long-lived; a token may
  expire mid-session.
* Backpressure: don't queue infinite frames per client; drop / disconnect stragglers.

## Rate limiting

* Sliding-window or token-bucket per-identity. Redis-backed for distributed apps.
* Different limits for different endpoint classes (login: 5/min per identity + IP; read: 100/s;
  write: 10/s). Publish limits in headers (`X-RateLimit-Remaining`, `Retry-After`).

## Privilege boundaries

* Least privilege: the app connects to the DB as a role that CAN'T `DROP TABLE`. The service
  writes to /var/lib/app but can't read /etc/shadow. The container runs as UID 1000, not root.
* Split: separate DB roles for migrations (schema changes) vs runtime (SELECT/INSERT).

## Sandboxing

* Executing user-supplied code: separate container, no network, RO filesystem except tmp, cpu +
  memory limits, wall-clock cap. Never in the main app process.
* Sali's execution broker + PolicyEngine + PathGuard exist for this. Never bypass them from
  within a skill or tool; that's the SINGLE authority for "may I do this".

## Dependency vulnerabilities

* Lockfiles (`package-lock.json`, `poetry.lock`, `composer.lock`) — reproducible installs.
* `npm audit`, `pip-audit`, `composer audit` in CI. Fail the build on high/critical.
* Automated dependency updates (dependabot / renovate) with grouped weekly PRs — do not accept
  daily noise; a stale-but-not-vulnerable dep is fine.
* GHSA / CVE watch on production dependencies.

## Container security

* Non-root user in the final image (`USER app`).
* Minimal base (`python:3.14-slim`, `node:22-alpine`) — smaller = fewer CVEs.
* No secrets baked in. Multi-stage build so build tools are absent from the runtime image.
* Read-only root filesystem where possible; tmpfs for `/tmp`.
* Health checks (`HEALTHCHECK` in Dockerfile OR compose-level).
* Scan images with Trivy / Grype in CI.

## Secure configuration

* HTTPS everywhere in production. Hardcode HTTPS redirects; HSTS with a real max-age.
* Cookies: `Secure; HttpOnly; SameSite=Lax` for sessions. Never `SameSite=None` without a real
  reason.
* No mixed content: HTTPS pages loading HTTP resources.
* CORS: explicit origin list, not `*`. `allow_credentials=True` requires an explicit origin.

## Logging

* NEVER log passwords, tokens, session ids, PII (email, phone, address, IP if regulated).
* Log the request ID, the identity id (a UUID), the action, the outcome. Never log the request
  body of a login/register/pay endpoint.
* Structured logs (JSON) go to a searchable backend. Grep-through-plaintext-log-files is fine
  for one machine and terrible at scale.
* Log ROTATION: hourly / daily rotation, retention policy, no unbounded log directories.

## Incident response

* Every service has a known KILL SWITCH — a way to revoke all sessions, rotate all secrets, and
  hard-refuse a specific attacker signature within minutes.
* Post-incident: write down what happened, how it was detected, what the fix was, and what the
  systemic change is. Not blame — LEARNING.
