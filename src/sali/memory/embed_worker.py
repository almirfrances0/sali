"""Background embedding.

Embeds memories whose ``embed_status='pending'`` using the configured embedder
(``nomic-embed-text``, served CPU-only so it never fights the reasoning model for VRAM —
fix H1/§M). Nomic needs asymmetric prefixes: documents are embedded with ``search_document:``
and queries with ``search_query:`` (fix L8).

A transient embed failure must never permanently drop a memory from recall (rule 7: decay
never deletes). On a batch failure we retry row-by-row so one poison input can't block the
rest, and any still-failing row is left ``pending`` to retry next cycle — never marked a
dead ``error``. (A per-row attempts cap is a Phase-7 hardening once a real worker runs.)
"""

from __future__ import annotations

from typing import Any

from sali.memory.retriever import vector_literal
from sali.provider.base import ModelProvider

DOC_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "

# A newly-embedded fact this close to an existing CURRENT one is either a restatement or a conflict —
# embeddings can't tell which (§46). We only SURFACE the pair for review, never auto-resolve.
_CONFLICT_SIM = 0.92


def _model_name(provider: ModelProvider) -> str:
    settings = getattr(provider, "s", None)
    return getattr(settings, "embed_model", "fake")


async def _store_vector(
    conn: Any, memory_id: Any, vec: list[float], model: str, layer: str | None = None
) -> None:
    await conn.execute(
        "UPDATE memory SET embedding=$1::vector, embed_model=$2, embed_status='done', "
        "  updated_at=now() WHERE id=$3",
        vector_literal(vec), model, memory_id,
    )
    if layer in ("semantic", "preference"):
        await _note_possible_conflict(conn, memory_id, vector_literal(vec))


async def _note_possible_conflict(conn: Any, memory_id: Any, vec_literal: str) -> None:
    """Free-text contradiction DETECTION (§13/§46): a fact very close to an existing current one might
    conflict with it. We can't tell a conflict from a restatement by embedding alone, so we only
    RECORD the pair (a durable signal Sali can review + resolve with memory_forget/verify) — we never
    silently supersede or merge."""
    near = await conn.fetchrow(
        "SELECT id, 1 - (embedding <=> $1::vector) AS sim FROM memory "
        "WHERE valid_until IS NULL AND superseded_by IS NULL AND embed_status='done' AND id <> $2 "
        "  AND layer IN ('semantic'::memory_layer, 'preference'::memory_layer) "
        "  AND embedding IS NOT NULL ORDER BY embedding <=> $1::vector LIMIT 1",
        vec_literal, memory_id,
    )
    if near is None or float(near["sim"]) < _CONFLICT_SIM:
        return
    await conn.execute(
        "INSERT INTO event (event_type, subject_type, subject_id, payload) "
        "VALUES ('memory.possible_conflict','memory',$1,$2)",
        memory_id, {"similar_to": str(near["id"]), "similarity": round(float(near["sim"]), 3)},
    )


async def embed_pending_conn(conn: Any, provider: ModelProvider, batch: int = 32) -> int:
    """Embed up to ``batch`` pending memories on a single connection; return count embedded."""
    rows = await conn.fetch(
        "SELECT id, content, layer::text AS layer FROM memory "
        "WHERE embed_status = 'pending' AND valid_until IS NULL "
        "ORDER BY created_at LIMIT $1",
        batch,
    )
    if not rows:
        return 0
    model = _model_name(provider)
    try:
        vectors = await provider.embed([DOC_PREFIX + r["content"] for r in rows])
    except Exception:  # noqa: BLE001 - fall back to per-row so one bad input can't poison the batch
        return await _embed_individually(conn, provider, rows, model)

    async with conn.transaction():
        for r, vec in zip(rows, vectors, strict=True):
            await _store_vector(conn, r["id"], vec, model, r["layer"])
    return len(rows)


async def _embed_individually(
    conn: Any, provider: ModelProvider, rows: list[Any], model: str
) -> int:
    embedded = 0
    for r in rows:
        try:
            vec = (await provider.embed([DOC_PREFIX + r["content"]]))[0]
        except Exception:  # noqa: BLE001 - leave this row 'pending' to retry next cycle
            continue
        await _store_vector(conn, r["id"], vec, model, r.get("layer", None))
        embedded += 1
    return embedded


async def embed_pending(pool: Any, provider: ModelProvider, batch: int = 32) -> int:
    """Pool wrapper around :func:`embed_pending_conn`."""
    async with pool.acquire() as conn:
        return await embed_pending_conn(conn, provider, batch)
