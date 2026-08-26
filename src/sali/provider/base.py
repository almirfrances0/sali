"""The model-agnostic provider contract.

``context/`` and ``retrieval/`` depend only on this module — never on a concrete backend —
so swapping Ollama for another engine is a single registry edit (engineering rule 10).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(slots=True)
class ToolSpec:
    """A tool advertised to the model (JSON-Schema parameters)."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(slots=True)
class ToolCall:
    """A tool invocation the model asked for."""

    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class ChatMessage:
    """One message in a chat exchange. ``name`` carries the tool name on tool results."""

    role: str
    content: str
    name: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass(slots=True)
class ChatResult:
    """A model response: final content, optional reasoning, any tool calls, token counts."""

    content: str
    thinking: str | None
    tool_calls: list[ToolCall]
    tokens_in: int
    tokens_out: int
    model: str


@runtime_checkable
class ModelProvider(Protocol):
    """Everything Sali needs from a reasoning backend."""

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec] | None = None,
        options: dict[str, Any] | None = None,
        think: bool = False,
    ) -> ChatResult: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    def count_tokens(self, text: str) -> int: ...

    async def health(self) -> bool: ...
