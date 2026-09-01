---
name: PostgreSQL
tags:
  - postgresql
  - postgres
  - psql
  - sql
  - database
  - migration
---

# PostgreSQL

## Connecting
- `psql -U <user> -d <db>` (peer auth over the unix socket needs no password when the OS user matches the role).
- List: `\l` databases, `\dt` tables, `\d <table>` schema.

## Schema & migrations
- Forward-only migrations; never edit an applied one — add a new numbered file.
- Wrap DDL in a transaction where the tool allows, so a failed migration leaves no partial schema.
- Index the columns you filter/join on; add `UNIQUE`/`CHECK` constraints for invariants.

## Verification
- The migration applied (the table/column/constraint exists — confirm with `\d`), and a representative query returns the expected rows. That is the evidence a schema change worked.

## Gotchas
- Enum columns: cast values explicitly (`$1::my_enum`).
- Connection refused → the server isn't running or the socket path/host is wrong.
- Permission denied → the role lacks the grant, or peer auth mismatches the OS user.
