"""Strategy learning — reusable reasoning approaches that apply across many tasks.

Sali learns procedures (how to do X) from tool execution patterns. Strategies are higher-level:
they are APPROACHES to reasoning that apply across unrelated problems.

Examples:
- "diagnose before modifying" — inspect observed state before applying changes
- "verify assumptions before execution" — check that what you believe is still true
- "test the smallest hypothesis first" — start with the cheapest experiment
- "compare competing explanations" — don't commit to the first theory
- "use previous failure evidence" — check what failed before repeating an approach

Strategies are evidence-backed: each tracks how many times it was used and how many
succeeded. Confidence derives from the success ratio. Strategies that repeatedly fail
are degraded; strategies that consistently succeed are promoted.

Sali is a digital person. Strategies are how she learns to THINK better, not just DO things.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from sali.obs.log import get_logger

log = get_logger("sali.strategy")


class StrategyStore:
    """Manages Sali's learned reasoning strategies."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def record(
        self,
        name: str,
        description: str,
        *,
        context: str = "",
        source: str = "experience",
    ) -> str:
        """Record a new strategy or reinforce an existing one. Returns the strategy ID."""
        strategy_id = str(uuid4())
        async with self._pool.acquire() as conn:
            # Check if this strategy already exists (dedup by name)
            existing = await conn.fetchval(
                "SELECT id FROM sali.memory "
                "WHERE claim_key = $1 AND valid_until IS NULL "
                "AND layer = 'procedural'::sali.memory_layer",
                f"strategy:{name}")
            if existing:
                # Reinforce: bump evidence count
                await conn.execute(
                    "UPDATE sali.memory SET evidence_count = evidence_count + 1, "
                    "last_seen = now(), last_verified = now(), updated_at = now() "
                    "WHERE id = $1", existing)
                return str(existing)

            # New strategy — store as a procedural memory with strategy metadata
            content = f"Strategy: {name} — {description}"
            if context:
                content += f" (applies when: {context})"
            await conn.execute(
                "INSERT INTO sali.memory "
                "(id, layer, content, source, claim_key, functional, confidence, importance, "
                "reliability, evidence_count, freshness, needs_grounding, scope, protected, "
                "valid_from, valid_until, structured) "
                "VALUES ($1, 'procedural'::sali.memory_layer, $2, $3::sali.memory_source, "
                "$4, true, 0.6, 0.7, 0.6, 1, 'slow'::sali.freshness_policy, false, 'global', false, "
                "now(), NULL, $5)",
                uuid4(), content, source, f"strategy:{name}",
                {"type": "strategy", "name": name, "context": context,
                 "uses": 1, "successes": 0})
        return strategy_id

    async def record_outcome(self, name: str, *, success: bool) -> None:
        """Record whether using this strategy led to success or failure."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, structured FROM sali.memory "
                "WHERE claim_key = $1 AND valid_until IS NULL "
                "AND layer = 'procedural'::sali.memory_layer",
                f"strategy:{name}")
            if row is None:
                return
            structured = dict(row["structured"] or {})
            uses = int(structured.get("uses", 0)) + 1
            successes = int(structured.get("successes", 0)) + (1 if success else 0)
            structured["uses"] = uses
            structured["successes"] = successes
            confidence = min(0.95, 0.4 + 0.05 * successes) if uses > 0 else 0.5
            await conn.execute(
                "UPDATE sali.memory SET structured = $1, confidence = $2, "
                "evidence_count = $3, last_seen = now(), updated_at = now() "
                "WHERE id = $4",
                structured, confidence, uses, row["id"])

    async def relevant_strategies(self, query: str, *, limit: int = 3) -> list[dict[str, Any]]:
        """Find strategies relevant to a query. Used during context assembly."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT content, confidence, structured FROM sali.memory "
                "WHERE claim_key LIKE 'strategy:%' AND valid_until IS NULL "
                "AND layer = 'procedural'::sali.memory_layer "
                "AND confidence >= 0.5 "
                "ORDER BY confidence DESC, evidence_count DESC "
                "LIMIT $1", limit)
        return [{
            "name": (r["structured"] or {}).get("name", ""),
            "content": r["content"],
            "confidence": float(r["confidence"]),
            "uses": (r["structured"] or {}).get("uses", 0),
            "successes": (r["structured"] or {}).get("successes", 0),
        } for r in rows]

    async def all_strategies(self) -> list[dict[str, Any]]:
        """List all known strategies with their evidence."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT content, confidence, structured FROM sali.memory "
                "WHERE claim_key LIKE 'strategy:%' AND valid_until IS NULL "
                "AND layer = 'procedural'::sali.memory_layer "
                "ORDER BY confidence DESC")
        return [{
            "name": (r["structured"] or {}).get("name", ""),
            "content": r["content"],
            "confidence": float(r["confidence"]),
            "uses": (r["structured"] or {}).get("uses", 0),
            "successes": (r["structured"] or {}).get("successes", 0),
        } for r in rows]
