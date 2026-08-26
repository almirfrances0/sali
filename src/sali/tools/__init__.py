"""Tools — Sali's hands. Every tool is deterministic Python; none calls an LLM."""

from sali.tools.base import Tool, ToolResult, ToolValidationError, VerifyResult
from sali.tools.registry import ToolRegistry, default_registry

__all__ = [
    "Tool",
    "ToolRegistry",
    "ToolResult",
    "ToolValidationError",
    "VerifyResult",
    "default_registry",
]
