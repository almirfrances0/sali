"""Tool dispatch: validate arguments against the tool's JSON Schema *before* any effect,
then execute with a backstop timeout. Never raises — always returns a ToolResult."""

from __future__ import annotations

import asyncio
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema import ValidationError as SchemaError

from sali.tools.base import Tool, ToolResult
from sali.tools.context import ToolContext


def validate_args(parameters: dict[str, Any], args: dict[str, Any]) -> None:
    """Full JSON-Schema (draft 2020-12) validation; raises SchemaError on any violation."""
    Draft202012Validator(parameters).validate(args)


async def run_tool(tool: Tool, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    try:
        validate_args(tool.parameters, args)
    except SchemaError as exc:
        return ToolResult(ok=False, display="invalid arguments", error=f"schema: {exc.message}")
    try:
        return await asyncio.wait_for(tool.run(args, ctx), timeout=tool.timeout_s + 2.0)
    except TimeoutError:
        return ToolResult(ok=False, display="timeout", error=f"{tool.name} timed out")
    except Exception as exc:  # noqa: BLE001 - a failing tool must never crash the loop
        return ToolResult(ok=False, display="error", error=f"{type(exc).__name__}: {exc}")
