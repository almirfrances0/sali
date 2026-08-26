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
    """Sali's built-in tools (system, filesystem, git, exec, web, remember, relate, tasks)."""
    from sali.tools.builtins import (
        browser_tool,
        comms_tool,
        exec_tool,
        filesystem,
        git,
        graph_tool,
        ingest_tool,
        memory_tool,
        notify_tool,
        perception_tool,
        schedule_tool,
        ssh,
        system,
        task_tool,
        vision_tool,
        web,
    )

    registry = ToolRegistry()
    for module in (system, filesystem, git, exec_tool, web, memory_tool, graph_tool, task_tool,
                   schedule_tool, ingest_tool, ssh, comms_tool, browser_tool, notify_tool,
                   vision_tool, perception_tool):
        module.register_builtins(registry)
    return registry
