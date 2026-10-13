#!/usr/bin/env bash
# Tier-1 logical backup of Sali's datastore. Runs as `almir` over peer auth — no sudo,
# no password. Memory is precious (engineering rule 17): this runs from Phase 2 onward.
set -euo pipefail

DIR="${SALI_BACKUP_DIR:-$HOME/.local/share/sali/backups}"
KEEP="${SALI_BACKUP_KEEP:-14}"
mkdir -p "$DIR"
ts="$(date +%Y%m%d-%H%M%S)"

# Custom-format dump (compressed, selectively restorable) + globals (roles/grants; fix L3).
pg_dump -Fc -Z6 -d sali -f "$DIR/sali-$ts.dump"
pg_dumpall --globals-only --no-role-passwords -f "$DIR/globals-$ts.sql"

# Verify the dump is readable before trusting it.
pg_restore -l "$DIR/sali-$ts.dump" >/dev/null

# GFS-lite retention: keep the newest $KEEP of each artifact.
ls -1t "$DIR"/sali-*.dump   2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f
ls -1t "$DIR"/globals-*.sql 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f

echo "[✓] backup: $DIR/sali-$ts.dump"
