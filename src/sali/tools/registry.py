"""The tool registry — the model is offered only what is registered *and* available."""

from __future__ import annotations

from sali.provider.base import ToolSpec
from sali.tools.base import Tool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def advertise(self) -> list[ToolSpec]:
        """Specs offered to the model — available tools only (discover-on-install stays hidden)."""
        return [t.spec() for t in self._tools.values() if t.available]

    def __len__(self) -> int:
        return len(self._tools)


def default_registry() -> ToolRegistry:
    """A registry with Sali's built-in R0 observation tools."""
    from sali.tools.builtins.system import register_builtins

    registry = ToolRegistry()
    register_builtins(registry)
    return registry
