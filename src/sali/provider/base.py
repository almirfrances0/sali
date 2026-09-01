"""The model-agnostic provider contract.

``context/`` and ``retrieval/`` depend only on this module — never on a concrete backend —
so swapping Ollama for another engine is a single registry edit (engineering rule 10).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
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


@dataclass(slots=True)
class ChatChunk:
    """A streamed piece of a response: a content/thinking delta, and on ``done`` the full result."""

    content: str = ""
    thinking: str = ""
    done: bool = False
    result: ChatResult | None = None


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

    def chat_stream(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec] | None = None,
        options: dict[str, Any] | None = None,
        think: bool = False,
    ) -> AsyncIterator[ChatChunk]: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    async def describe_image(self, prompt: str, image: bytes) -> str:
        """Reason over an image (a screenshot) LOCALLY with the vision model — never leaves the
        machine (sali3 §24-25). Returns the model's answer about what the image shows."""
        ...

    def count_tokens(self, text: str) -> int: ...

    def context_limit(self) -> int | None:
        """The model's effective context window in tokens, or None if the backend can't report one
        (the runtime then falls back to the configured limit — never assumes it's unbounded)."""
        ...

    async def health(self) -> bool: ...
