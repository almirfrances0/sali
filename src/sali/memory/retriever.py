"""Low-level retrievers over *current* memory (``valid_until IS NULL``).

Two independent angles, fused in the service: lexical (trigram ILIKE) and semantic (pgvector
cosine over the HNSW index). Each query joins ``layer_policy`` so the service can apply
half-life decay to importance without a second round-trip.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

_HALF_LIFE = "extract(epoch FROM lp.importance_half_life) AS half_life_s"
_FROM = "FROM memory m JOIN layer_policy lp ON lp.layer = m.layer"

# Common words carry no retrieval signal; drop them so a query matches on its content terms.
_KW_STOP = frozenset({
    "the", "and", "for", "what", "did", "say", "about", "you", "your", "this", "that", "with",
    "from", "how", "are", "was", "were", "does", "which", "who", "why", "have", "has", "any",
})


def vector_literal(vec: Sequence[float]) -> str:
    """Render an embedding as a pgvector text literal, e.g. '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{x:.7f}" for x in vec) + "]"


def query_terms(query: str) -> list[str]:
    """Significant lowercase terms (≥3 chars, minus stopwords) for lexical matching."""
    return [w for w in re.findall(r"[a-z0-9]{3,}", query.lower()) if w not in _KW_STOP]


async def retrieve_keyword(conn: Any, query: str, limit: int = 10) -> list[Any]:
    terms = query_terms(query)
    if not terms:
        return []
    patterns = [f"%{t}%" for t in terms]
    sql = (
        f"SELECT m.*, {_HALF_LIFE} {_FROM} "
        "WHERE m.valid_until IS NULL AND m.content ILIKE ANY($1) "
        "ORDER BY m.importance DESC, m.confidence DESC, m.id LIMIT $2"  # m.id = stable tiebreak
    )
    return list(await conn.fetch(sql, patterns, limit))


async def retrieve_vector(conn: Any, query_vec: Sequence[float], limit: int = 10) -> list[Any]:
    literal = vector_literal(query_vec)
    sql = (
        f"SELECT m.*, {_HALF_LIFE}, (1 - (m.embedding <=> $1::vector)) AS similarity {_FROM} "
        "WHERE m.valid_until IS NULL AND m.embed_status = 'done' AND m.embedding IS NOT NULL "
        "ORDER BY m.embedding <=> $1::vector, m.id LIMIT $2"  # m.id = stable tiebreak
    )
    return list(await conn.fetch(sql, literal, limit))
