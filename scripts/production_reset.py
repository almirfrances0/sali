#!/usr/bin/env python3
"""Production-state reset (Final audit §45-§48).

Removes Sali's DEV/CONSTRUCTION-generated LIFE EXPERIENCE while preserving its DNA — schema, migrations,
config seeds, and (via `sali init` afterward) its seeded identity + machine knowledge. After this, Sali is a
clean, long-lived instance ready to accumulate its OWN experiences, not a development project full of
memories created while it was being built.

CLASSIFICATION (the important part — never a blind `DELETE FROM memory`):
  • PRESERVED, never touched:  schema_migrations (the migration ledger), layer_policy + freshness_rule
    (config/DNA seeded inside migrations that `sali init` will NOT re-insert because those migrations are
    already applied).
  • RESET to their clean singleton seed:  sali_state, execution_lease.
  • TRUNCATED (dev-generated operational + life + knowledge):  every other table in schema `sali` —
    tasks/runs/tools/messages/conversations/events, memory + graph + experiences + learning, goals/
    initiatives/commitments/obligations, capabilities, devices/sessions, etc. `event` is append-only
    (a row trigger forbids DELETE) so it is cleared with TRUNCATE, which the reset does anyway.

The IDENTITY seed (Almir/Sali graph + IDENTITY memory) and the desktop twin are intentionally NOT reinstated
here — run `sali init` after this script to restore them idempotently from the canonical seed.

SAFETY: refuses to run without --yes. Run pg_dump first (the Makefile `prod-reset` target does). --dry-run
prints exactly what it would preserve/reset/truncate and changes nothing.
"""

from __future__ import annotations

import argparse
import asyncio

# DNA seeded inside migrations that `sali init` will not re-insert (migrations already applied) → preserve.
_PRESERVE = frozenset({"schema_migrations", "layer_policy", "freshness_rule"})
# Singletons reset to their canonical clean seed rather than left with dev state.
_SINGLETON_RESET = frozenset({"sali_state", "execution_lease"})


async def _reset(database: str, *, dry_run: bool) -> None:
    from sali.config.settings import load_settings
    from sali.db.pool import connect

    settings = load_settings()
    settings = settings.model_copy(update={"db": settings.db.model_copy(update={"name": database})})
    conn = await connect(settings)
    try:
        tables = [
            r["table_name"] for r in await conn.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='sali' AND table_type='BASE TABLE' ORDER BY table_name")
        ]
        truncate = [t for t in tables if t not in _PRESERVE and t not in _SINGLETON_RESET]

        print(f"database: {database}")
        print(f"PRESERVE ({len(_PRESERVE)}): {', '.join(sorted(_PRESERVE))}")
        print(f"RESET singleton ({len(_SINGLETON_RESET)}): {', '.join(sorted(_SINGLETON_RESET))}")
        print(f"TRUNCATE ({len(truncate)}): {', '.join(truncate)}")

        if dry_run:
            print("\n[dry-run] no changes made.")
            return

        async with conn.transaction():
            # One CASCADE truncate handles all FK ordering + resets identity sequences.
            await conn.execute(
                f"TRUNCATE {', '.join(truncate)} RESTART IDENTITY CASCADE")
            # Re-seed the singletons to their clean canonical state.
            await conn.execute("TRUNCATE sali_state")
            await conn.execute("INSERT INTO sali_state (id) VALUES (true) ON CONFLICT DO NOTHING")
            await conn.execute("TRUNCATE execution_lease")
            await conn.execute(
                "INSERT INTO execution_lease (id, owner_id, run_id, session_id, status) "
                "VALUES ('foreground', 'none', gen_random_uuid(), gen_random_uuid(), 'releasing') "
                "ON CONFLICT (id) DO NOTHING")
        print(f"\n[done] reset {len(truncate)} tables + 2 singletons. "
              f"Run `sali init` to restore the identity seed + desktop twin.")
    finally:
        await conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Reset Sali to a clean production instance (preserves DNA).")
    ap.add_argument("--database", default="sali", help="target database (default: sali)")
    ap.add_argument("--yes", action="store_true", help="actually perform the reset (required)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan; change nothing")
    args = ap.parse_args()
    if not args.yes and not args.dry_run:
        ap.error("refusing to reset without --yes (or use --dry-run to preview). Back up first!")
    asyncio.run(_reset(args.database, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
