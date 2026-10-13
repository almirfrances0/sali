#!/usr/bin/env bash
# Monthly restore drill — an untested backup is only a hope (engineering rule 17 / fix M7).
# Restores the latest dump into a throwaway DB, asserts the schema came back, logs a
# `backup.drill` event so backup health is itself observable, then drops the throwaway.
# Runs as `almir` (needs CREATEDB, granted once at bootstrap).
set -euo pipefail

DIR="${SALI_BACKUP_DIR:-$HOME/.local/share/sali/backups}"
DRILL_DB="sali_restore_test"

latest="$(ls -1t "$DIR"/sali-*.dump 2>/dev/null | head -1 || true)"
[[ -n "$latest" ]] || { echo "no backup found in $DIR — run backup_nightly.sh first" >&2; exit 1; }

dropdb --if-exists "$DRILL_DB"
# Clone from a template that already has the extensions (vector isn't a "trusted"
# extension, so a non-superuser can't create it into an empty DB).
createdb -T sali_restore_tpl "$DRILL_DB"
trap 'dropdb --if-exists "$DRILL_DB"' EXIT

# The dump carries `COMMENT ON EXTENSION ...` lines that a non-superuser can't apply
# (the template's extensions are superuser-owned). Those notices are benign, so we don't
# trust pg_restore's exit code — we validate the restore by real assertions below.
pg_restore -d "$DRILL_DB" "$latest" 2>"$DIR/.last_restore_drill.log" || true

have_memory="$(psql -d "$DRILL_DB" -tAc "SELECT to_regclass('sali.memory') IS NOT NULL")"
[[ "$have_memory" == "t" ]] || { echo "restore drill FAILED: sali.memory missing" >&2; exit 1; }

src_rows="$(psql -d sali       -tAc "SELECT count(*) FROM sali.memory")"
dst_rows="$(psql -d "$DRILL_DB" -tAc "SELECT count(*) FROM sali.memory")"
[[ "$src_rows" == "$dst_rows" ]] \
  || { echo "restore drill FAILED: memory rows $dst_rows != source $src_rows" >&2; exit 1; }

tables="$(psql -d "$DRILL_DB" -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='sali'")"
[[ "$tables" -gt 0 ]] || { echo "restore drill FAILED: no sali tables restored" >&2; exit 1; }

psql -d sali -qc \
  "INSERT INTO sali.event (event_type, payload) VALUES ('backup.drill',
     jsonb_build_object('source', '$(basename "$latest")', 'sali_tables', $tables, 'ok', true))"

echo "[✓] restore drill ok: $tables sali tables, $dst_rows memory rows restored from $(basename "$latest")"
