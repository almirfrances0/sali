"""Tool dispatch: validate arguments *before* any effect, then run under a hard timeout.

Argument validation here is a lightweight required-field check; full JSON-Schema validation
arrives with the effectful tools in a later phase.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sali.tools.base import Tool, ToolResult, ToolValidationError


def validate_args(parameters: dict[str, Any], args: dict[str, Any]) -> None:
    if not isinstance(args, dict):
        raise ToolValidationError("arguments must be an object")
    required = parameters.get("required", [])
    missing = [key for key in required if key not in args]
    if missing:
        raise ToolValidationError(f"missing required argument(s): {', '.join(missing)}")


async def run_tool(tool: Tool, args: dict[str, Any]) -> ToolResult:
    """Validate, then execute with a backstop timeout; never raises — returns a ToolResult."""
    try:
        validate_args(tool.parameters, args)
    except ToolValidationError as exc:
        return ToolResult(ok=False, display="invalid arguments", error=str(exc))
    try:
        return await asyncio.wait_for(tool.run(args), timeout=tool.timeout_s + 2.0)
    except TimeoutError:
        return ToolResult(ok=False, display="timeout", error=f"{tool.name} timed out")
    except Exception as exc:  # noqa: BLE001 - a failing tool must never crash the loop
        return ToolResult(ok=False, display="error", error=f"{type(exc).__name__}: {exc}")
