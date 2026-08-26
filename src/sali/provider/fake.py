"""A deterministic in-memory provider for tests and offline wiring.

Satisfies ``ModelProvider`` structurally, records every call, returns scripted responses,
and produces stable unit-norm embeddings from a hash — so nothing in the test suite ever
touches Ollama (engineering: deterministic fixtures).
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import AsyncIterator
from typing import Any

from sali.provider.base import ChatChunk, ChatMessage, ChatResult, ToolSpec


class FakeModelProvider:
    def __init__(self, *, dim: int = 768, responses: list[ChatResult] | None = None) -> None:
        self.dim = dim
        self._responses: list[ChatResult] = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec] | None = None,
        options: dict[str, Any] | None = None,
        think: bool = False,
    ) -> ChatResult:
        self.calls.append({"messages": messages, "tools": tools, "options": options})
        if self._responses:
            return self._responses.pop(0)
        prompt = " ".join(m.content for m in messages)
        return ChatResult(
            content="(fake response)",
            thinking=None,
            tool_calls=[],
            tokens_in=self.count_tokens(prompt),
            tokens_out=3,
            model="fake",
        )

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec] | None = None,
        options: dict[str, Any] | None = None,
        think: bool = False,
    ) -> AsyncIterator[ChatChunk]:
        result = await self.chat(messages, tools=tools, options=options, think=think)
        for word in result.content.split(" "):
            if word:
                yield ChatChunk(content=word + " ")
        yield ChatChunk(done=True, result=result)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def _vector(self, text: str) -> list[float]:
        seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
        rng = random.Random(seed)
        raw = [rng.gauss(0.0, 1.0) for _ in range(self.dim)]
        norm = math.sqrt(sum(x * x for x in raw)) or 1.0
        return [x / norm for x in raw]

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    async def health(self) -> bool:
        return True
