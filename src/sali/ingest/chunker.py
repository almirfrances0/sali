"""Split document text into overlapping chunks sized for embedding + recall.

Prefer to break on paragraph/line boundaries so a chunk stays coherent; fall back to a hard split
for a runaway line. Overlap keeps a fact that straddles a boundary retrievable from either chunk.
"""

from __future__ import annotations

_CHUNK_CHARS = 1200
_OVERLAP_CHARS = 150


def chunk_text(text: str, *, size: int = _CHUNK_CHARS, overlap: int = _OVERLAP_CHARS) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    # Break on blank lines first (paragraphs), then single newlines, so chunks are coherent.
    units = [u for u in _split_units(text) if u]
    chunks: list[str] = []
    current = ""
    for unit in units:
        if current and len(current) + len(unit) + 1 > size:
            chunks.append(current)
            current = (current[-overlap:] + "\n" + unit) if overlap else unit
        else:
            current = f"{current}\n{unit}" if current else unit
        # A single unit larger than the window: hard-split it.
        while len(current) > size:
            chunks.append(current[:size])
            current = current[size - overlap:] if overlap else current[size:]
    if current.strip():
        chunks.append(current)
    return chunks


def _split_units(text: str) -> list[str]:
    paras = text.split("\n\n")
    if len(paras) > 1:
        return [p.strip() for p in paras]
    return [line.strip() for line in text.split("\n")]
