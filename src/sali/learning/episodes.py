"""Memory consolidation (spec §19): fold raw short-term observations into a durable episode.

The ladder is raw event → short-term observation → episode → … Short-term observations pile up
in ``stm_observation`` (ephemeral, TTL'd); consolidation distills a batch of them into ONE
episodic memory (the INTERPRET step — the model summarizes) and clears the raw rows it folded.
Nothing reads STM directly, so folding it away is exactly its purpose — the messy room, organised.
"""

from __future__ import annotations

import json
import re
from typing import Any

from sali.core.enums import MemoryLayer, MemorySource
from sali.memory import writer as memory_writer
from sali.provider.presets import BALANCED
from sali.provider.base import ChatMessage, ModelProvider

_MIN_OBS = 5  # don't manufacture an "episode" from a couple of stray observations
# Brain-audit Turn 6: _EPISODE_OPTS deleted; preset=BALANCED used at call site.
_EPISODE_SYSTEM = (
    "You are Sali. These are your recent short-term observations. Decide whether anything here is "
    "worth keeping permanently, and answer with JSON only:\n"
    '{"keep": true, "episode": "<a few first-person sentences: what happened and what matters>", '
    '"importance": 0.0-1.0}\n'
    'or, when nothing here deserves to outlive the day: {"keep": false}\n'
    "Keep it when something happened that your future self would need: a decision, a correction, a "
    "result, something learned, something that changed. Do NOT keep greetings, acknowledgements, "
    "small talk, repetition of what you already know, or a summary of having answered a question. "
    "Importance: 0.2 routine, 0.5 useful, 0.8 changes how you work or what you believe."
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
            preset=BALANCED,
        )
        raw = res.content.strip()
    except Exception:  # noqa: BLE001 - distillation is best-effort; leave the raw for next time
        return 0
    # AN ABSTAIN PATH. This used to take whatever the model emitted and write it, unconditionally, at a
    # hardcoded importance of 0.4 — so every ~5 turns of any kind minted one permanent episodic memory,
    # whether or not anything had happened. That is "save everything" wearing a summariser, and it is the
    # thing that fills a lifetime memory with "I checked the system resources and answered a question".
    # The model may now decline, and may say how much a kept episode matters.
    episode, importance = _parse_episode(raw)
    if not episode:
        # Nothing worth keeping — still clear the raw, or it is re-offered forever.
        await conn.execute(
            "DELETE FROM stm_observation WHERE id = ANY($1::uuid[])", [r["id"] for r in rows])
        return 0
    # The model can distill the same summary twice (identical STM → identical episode). Skip if that
    # exact episode is already current — otherwise the insert trips ux_memory_current and aborts the
    # whole consolidation pass. Clear the folded raw either way so it doesn't pile up forever.
    already = await conn.fetchval(
        "SELECT 1 FROM memory WHERE layer='episodic'::memory_layer "
        "AND content_hash = digest($1, 'sha256') AND valid_until IS NULL LIMIT 1",
        episode,
    )
    if already:
        await conn.execute(
            "DELETE FROM stm_observation WHERE id = ANY($1::uuid[])", [r["id"] for r in rows])
        return 0
    await memory_writer.remember(
        conn, layer=MemoryLayer.EPISODIC, content=episode,
        source=MemorySource.CONVERSATION, importance=importance, obs_conf=0.7,
    )
    await conn.execute(
        "DELETE FROM stm_observation WHERE id = ANY($1::uuid[])", [r["id"] for r in rows]
    )
    return 1


def _parse_episode(raw: str) -> tuple[str, float]:
    """Read the model's verdict. Returns ("", 0.0) when it declined or said nothing usable.

    Tolerant on purpose: a Q2_K model will sometimes answer with prose instead of JSON. Prose that looks
    like an episode is still kept (at the old constant importance) rather than thrown away — the abstain
    path must not become an accidental amnesia path.
    """
    text = (raw or "").strip()
    if not text:
        return "", 0.0
    match = re.search(r"\{.*\}", text, re.S)
    if match is not None:
        try:
            data = json.loads(match.group(0))
        except ValueError:
            data = None
        if isinstance(data, dict):
            if not data.get("keep"):
                return "", 0.0
            episode = str(data.get("episode") or "").strip()
            try:
                importance = float(data.get("importance", 0.4))
            except (TypeError, ValueError):
                importance = 0.4
            return episode, min(1.0, max(0.05, importance))
    # Not JSON. If it reads like a refusal, honour it; otherwise treat it as the episode.
    if re.fullmatch(r"(no|none|nothing|nothing worth keeping|n/a)[.!]?", text, re.IGNORECASE):
        return "", 0.0
    return text, 0.4


async def prune_stm(conn: Any) -> int:
    """Drop short-term observations past their TTL — raw events that were never worth keeping."""
    result = await conn.execute("DELETE FROM stm_observation WHERE expires_at <= now()")
    try:
        return int(result.split()[-1])  # asyncpg returns e.g. "DELETE 7"
    except (ValueError, IndexError, AttributeError):
        return 0
