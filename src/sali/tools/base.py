"""The tool contract.

A ``Tool`` is deterministic Python with a declared risk level, capabilities, timeout, and a
verification step. It advertises itself to the model as a :class:`ToolSpec`; the model can
only ever ask for tools that are advertised. Effects are always verified (engineering
rule 13) — the default verification confirms the tool produced output; effectful tools
override :meth:`verify` with a real post-condition check.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from sali.core.enums import Capability, RiskLevel
from sali.provider.base import ToolSpec


class ToolValidationError(Exception):
    """Arguments failed validation *before* any effect ran."""


@dataclass(slots=True)
class ToolResult:
    ok: bool
    output: dict[str, Any] = field(default_factory=dict)
    display: str = ""
    error: str | None = None


@dataclass(slots=True)
class VerifyResult:
    success: bool
    detail: str


class Tool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    parameters: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}, "required": []}
    risk_level: ClassVar[RiskLevel] = RiskLevel.R0
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.READ})
    timeout_s: ClassVar[float] = 10.0
    idempotent: ClassVar[bool] = True
    available: ClassVar[bool] = True  # discover-on-install tools flip this off

    @abstractmethod
    async def run(self, args: dict[str, Any]) -> ToolResult: ...

    async def verify(self, args: dict[str, Any], result: ToolResult) -> VerifyResult:
        """Default post-condition: the tool succeeded and returned something."""
        if result.ok and result.output:
            return VerifyResult(True, "output present")
        return VerifyResult(False, result.error or "no output produced")

    @classmethod
    def spec(cls) -> ToolSpec:
        return ToolSpec(name=cls.name, description=cls.description, parameters=cls.parameters)
