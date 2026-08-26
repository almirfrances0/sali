"""Memory consolidation (spec §19): fold raw short-term observations into a durable episode.

The ladder is raw event → short-term observation → episode → … Short-term observations pile up
in ``stm_observation`` (ephemeral, TTL'd); consolidation distills a batch of them into ONE
episodic memory (the INTERPRET step — the model summarizes) and clears the raw rows it folded.
Nothing reads STM directly, so folding it away is exactly its purpose — the messy room, organised.
"""

from __future__ import annotations

from typing import Any

from sali.core.enums import MemoryLayer, MemorySource
from sali.memory import writer as memory_writer
from sali.provider.base import ChatMessage, ModelProvider

_MIN_OBS = 5  # don't manufacture an "episode" from a couple of stray observations
_EPISODE_OPTS: dict[str, Any] = {"temperature": 0.4, "top_k": 40, "top_p": 0.9}
_EPISODE_SYSTEM = (
    "You are Sali. Fold these recent short-term observations into ONE compact episodic memory — "
    "a few first-person sentences capturing what actually happened and what's worth remembering. "
    "Drop trivia and duplication. Just the episode, nothing else."
)


async def consolidate_stm(
    conn: Any, provider: ModelProvider, *, min_obs: int = _MIN_OBS, limit: int = 200
) -> int:
    """Distill a batch of live short-term observations into a durable episode; clear what's folded.
    Returns 1 if an episode was written, else 0. Caller owns the transaction."""
    rows = await conn.fetch(
        "SELECT id, content FROM stm_observation WHERE expires_at > now() "
        "ORDER BY created_at LIMIT $1",
        limit,
    )
    if len(rows) < min_obs:
        return 0  # not enough raw material to be worth an episode yet
    transcript = "\n".join(f"- {r['content']}" for r in rows)
    try:
        res = await provider.chat(
            [ChatMessage(role="system", content=_EPISODE_SYSTEM),
             ChatMessage(role="user", content=transcript)],
            options=_EPISODE_OPTS,
        )
        episode = res.content.strip()
    except Exception:  # noqa: BLE001 - distillation is best-effort; leave the raw for next time
        return 0
    if not episode:
        return 0
    await memory_writer.remember(
        conn, layer=MemoryLayer.EPISODIC, content=episode,
        source=MemorySource.CONVERSATION, importance=0.4, obs_conf=0.7,
    )
    await conn.execute(
        "DELETE FROM stm_observation WHERE id = ANY($1::uuid[])", [r["id"] for r in rows]
    )
    return 1


async def prune_stm(conn: Any) -> int:
    """Drop short-term observations past their TTL — raw events that were never worth keeping."""
    result = await conn.execute("DELETE FROM stm_observation WHERE expires_at <= now()")
    try:
        return int(result.split()[-1])  # asyncpg returns e.g. "DELETE 7"
    except (ValueError, IndexError, AttributeError):
        return 0
