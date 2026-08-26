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
from typing import TYPE_CHECKING, Any, ClassVar

from sali.core.enums import Capability, RiskLevel
from sali.provider.base import ToolSpec

if TYPE_CHECKING:
    from sali.tools.context import ToolContext


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
    # Fail CLOSED: a tool that forgets to declare its risk is treated as the most dangerous,
    # so an unclassified (e.g. discovered) tool is denied by default, never auto-allowed.
    risk_level: ClassVar[RiskLevel] = RiskLevel.R4
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.READ})
    # Outer backstop only (dispatch adds +2s). Generous by default so an I/O/network tool isn't
    # cut below its own internal timeout; local tools finish in milliseconds regardless. Tools with
    # genuinely long work (ssh, ingest, browser) raise it further.
    timeout_s: ClassVar[float] = 45.0
    idempotent: ClassVar[bool] = True
    available: ClassVar[bool] = True  # discover-on-install tools flip this off

    @abstractmethod
    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult: ...

    def assess(self, args: dict[str, Any]) -> RiskLevel:
        """Effective risk for THIS specific call. Override to judge by the arguments — e.g. a
        plain command runs free (low) while a destructive one (rm -rf, mkfs) escalates to R4 so
        it pauses. This is how Sali stays free for ordinary work but careful about destruction."""
        return self.risk_level

    async def verify(
        self, args: dict[str, Any], result: ToolResult, ctx: ToolContext
    ) -> VerifyResult:
        """Post-condition check AFTER the tool ran (spec §23: never assume success). The default
        just confirms it returned something; effectful tools OVERRIDE this to independently
        re-observe reality (does the file now exist? is it gone?) rather than trust their own ok
        flag. This verifies the effect — it never blocks the action, so Sali stays free."""
        if result.ok and result.output:
            return VerifyResult(True, "output present")
        return VerifyResult(False, result.error or "no output produced")

    @classmethod
    def spec(cls) -> ToolSpec:
        return ToolSpec(name=cls.name, description=cls.description, parameters=cls.parameters)
