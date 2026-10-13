#!/usr/bin/env bash
# One-time privileged setup for Sali's Postgres datastore.
#   sudo bash scripts/bootstrap_db.sh
# Creates a peer-auth role `almir` (NO password), databases `sali` and `sali_test`
# owned by it, and the required extensions. Idempotent — safe to re-run.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: run as root, e.g.  sudo bash scripts/bootstrap_db.sh" >&2
  exit 1
fi

PSQL() { runuser -u postgres -- psql -v ON_ERROR_STOP=1 "$@"; }

echo "[*] Ensuring role 'almir' (LOGIN, peer auth, no password)…"
if ! PSQL -tAc "SELECT 1 FROM pg_roles WHERE rolname='almir'" | grep -q 1; then
  PSQL -c "CREATE ROLE almir LOGIN"
fi

for db in sali sali_test; do
  echo "[*] Ensuring database '$db' owned by almir…"
  if ! PSQL -tAc "SELECT 1 FROM pg_database WHERE datname='$db'" | grep -q 1; then
    PSQL -c "CREATE DATABASE $db OWNER almir"
  fi
done

echo "[*] Installing pgvector (postgresql-18-pgvector) if missing…"
if ! dpkg -s postgresql-18-pgvector >/dev/null 2>&1; then
  if apt-get update -qq && apt-get install -y -qq postgresql-18-pgvector; then
    echo "[✓] pgvector installed."
  else
    echo "[!] pgvector install failed (network?). Not fatal for Phase 0; needed by Phase 2."
  fi
fi

echo "[*] Creating extensions + granting schema use in each database…"
for db in sali sali_test; do
  PSQL -d "$db" -c "CREATE EXTENSION IF NOT EXISTS pgcrypto;"
  PSQL -d "$db" -c "CREATE EXTENSION IF NOT EXISTS pg_trgm;"
  if dpkg -s postgresql-18-pgvector >/dev/null 2>&1; then
    PSQL -d "$db" -c "CREATE EXTENSION IF NOT EXISTS vector;"
  fi
  PSQL -d "$db" -c "GRANT ALL ON SCHEMA public TO almir;"
done

echo "[*] Ensuring restore-drill template 'sali_restore_tpl' (extensions pre-installed)…"
if ! PSQL -tAc "SELECT 1 FROM pg_database WHERE datname='sali_restore_tpl'" | grep -q 1; then
  PSQL -c "CREATE DATABASE sali_restore_tpl OWNER almir"
fi
PSQL -d sali_restore_tpl -c "CREATE EXTENSION IF NOT EXISTS pgcrypto;"
PSQL -d sali_restore_tpl -c "CREATE EXTENSION IF NOT EXISTS pg_trgm;"
if dpkg -s postgresql-18-pgvector >/dev/null 2>&1; then
  PSQL -d sali_restore_tpl -c "CREATE EXTENSION IF NOT EXISTS vector;"
fi
PSQL -c "ALTER DATABASE sali_restore_tpl IS_TEMPLATE true;"

echo "[✓] Bootstrap complete. Databases: sali, sali_test (+ sali_restore_tpl template)."
