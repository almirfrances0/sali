"""The 'interpret' branch of event-driven observation (spec §16).

Observation *stores* structural changes as ``twin.entity_added`` / ``twin.entity_removed``
events. This is where Sali gets to *notice* them: on its next turn it reads the changes it
hasn't acknowledged yet, so it can bring them up naturally ("btw, Docker got installed"). The
watermark is kept in the event log itself (a ``twin.acknowledged`` event carrying the last seq
seen) — no extra table. Changes are deduped by entity, last event winning, so an add-then-remove
churn resolves to its current state instead of two contradictory mentions.
"""

from __future__ import annotations

from typing import Any


def _phrase(added: bool, key: str, name: str | None) -> str:
    label = name or key.split(":", 1)[-1]
    kind = key.split(":", 1)[0]
    if not added:
        return f"{label} is gone"
    if kind == "software":
        return f"{label} was installed"
    if kind == "model":
        return f"the model {label} was pulled"
    if kind == "project":
        return f"a project appeared — {label}"
    if kind == "hw":
        return f"new hardware — {label}"
    return f"{label} showed up"


async def unacknowledged_changes(conn: Any) -> tuple[list[str], int]:
    """Return (human phrases, watermark seq) for machine changes Sali hasn't noticed yet."""
    through = await conn.fetchval(
        "SELECT coalesce(max((payload->>'through_seq')::bigint), 0) "
        "FROM event WHERE event_type='twin.acknowledged'"
    ) or 0
    rows = await conn.fetch(
        "SELECT e.seq, e.event_type, e.payload->>'key' AS key, n.name FROM event e "
        "LEFT JOIN graph_node n ON n.canonical_key = e.payload->>'key' AND n.valid_until IS NULL "
        "WHERE e.event_type IN ('twin.entity_added','twin.entity_removed') AND e.seq > $1 "
        "ORDER BY e.seq",
        int(through),
    )
    if not rows:
        return [], int(through)
    latest: dict[str, Any] = {}  # dedup by entity; insertion-ordered, later seq overwrites
    max_seq = int(through)
    for r in rows:
        latest[r["key"]] = r
        max_seq = max(max_seq, r["seq"])
    phrases = [_phrase(r["event_type"] == "twin.entity_added", r["key"], r["name"])
               for r in latest.values()]
    return phrases, max_seq


async def acknowledge(conn: Any, through_seq: int) -> None:
    """Mark machine changes up to ``through_seq`` as noticed, so they aren't surfaced again."""
    await conn.execute(
        "INSERT INTO event (event_type, subject_type, payload) VALUES "
        "('twin.acknowledged','machine',$1)",
        {"through_seq": int(through_seq)},
    )


async def unacknowledged_observations(conn: Any, *, limit: int = 8) -> tuple[list[str], int]:
    """Recent DESKTOP observations Sali hasn't noticed yet (from the continuous event engine, sali3
    Phase 5) — what Almir's been doing on screen: edits, app switches. Returns (phrases, watermark)."""
    through = await conn.fetchval(
        "SELECT coalesce(max((payload->>'through_seq')::bigint), 0) "
        "FROM event WHERE event_type='desktop.acknowledged'"
    ) or 0
    rows = await conn.fetch(
        "SELECT seq, payload->>'summary' AS summary FROM event "
        "WHERE event_type='desktop.observed' "
        # Exclude machine-firehose observations (new sockets / failed services / disk pressure) — those are
        # NOT 'what Almir's doing on screen' and were leaking into every turn as 'new listening socket …',
        # making Sali raise UDP/port topics unprompted. Mirrors the filter already in world_state.py.
        "AND coalesce(payload->>'kind','') NOT IN ('port_opened','service_failed','disk_pressure') "
        "AND seq > $1 ORDER BY seq DESC LIMIT $2",
        int(through), int(limit),
    )
    if not rows:
        return [], int(through)
    max_seq = max(int(through), max(int(r["seq"]) for r in rows))
    phrases = [str(r["summary"]) for r in rows if r["summary"]]  # newest first
    return phrases, max_seq


async def acknowledge_observations(conn: Any, through_seq: int) -> None:
    """Mark desktop observations up to ``through_seq`` as noticed, so they aren't surfaced again."""
    await conn.execute(
        "INSERT INTO event (event_type, subject_type, payload) VALUES "
        "('desktop.acknowledged','desktop',$1)",
        {"through_seq": int(through_seq)},
    )
