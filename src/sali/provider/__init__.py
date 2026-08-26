"""Model provider abstraction — Sali is model-agnostic (engineering rule 10)."""

from sali.provider.base import ChatMessage, ChatResult, ModelProvider, ToolCall, ToolSpec
from sali.provider.presets import BALANCED, CREATIVE, DETERMINISTIC, SamplingPreset
from sali.provider.registry import build_provider

__all__ = [
    "BALANCED",
    "CREATIVE",
    "DETERMINISTIC",
    "ChatMessage",
    "ChatResult",
    "ModelProvider",
    "SamplingPreset",
    "ToolCall",
    "ToolSpec",
    "build_provider",
]
