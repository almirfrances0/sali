"""Tool Intelligence — Increment 10: enforced execution + LLM interpretation (§34/§35/§88).

The authority record now GATES execution on the single policy path: a command whose binary is
classified system-critical pauses to confirm, even when the tool would auto-allow it. And the model
may only ESCALATE a tool's authority, never weaken the deterministic floor.
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.core.enums import ExecAuthority, RiskLevel
from sali.provider.base import ChatResult
from sali.provider.fake import FakeModelProvider
from sali.runtime.loop import _command_binary
from sali.security.policy import Action, PolicyDecision
from sali.twin.interpret import interpret_authority

pytestmark = pytest.mark.db


def test_command_binary_only_applies_to_command_tools() -> None:
    assert _command_binary("execute_command", {"command": "sudo mkfs /dev/sdb"}) == "mkfs"
    assert _command_binary("ssh_run", {"command": "fdisk /dev/sda"}) == "fdisk"
    assert _command_binary("read_file", {"command": "mkfs"}) is None   # not a command tool
    assert _command_binary("execute_command", {"path": "/x"}) is None  # no command arg


# ---- enforcement (loop authority floor) ------------------------------------------------------

async def test_system_critical_binary_forces_confirm(live_pool: Any) -> None:
    from sali.twin.authority import classify_tools
    from sali.twin.tools import sync_tools

    async with live_pool.acquire() as conn:
        await sync_tools(conn, {"mkfs": "/usr/sbin/mkfs", "ls": "/usr/bin/ls"}, {})
        await classify_tools(conn)

    loop = _make_loop(live_pool)
    exec_tool = loop.registry.get("execute_command")

    # a normal command auto-allows…
    ok = loop.policy.decide(exec_tool, {"command": "ls -la"})
    ok = await loop._authority_floor(exec_tool, {"command": "ls -la"}, ok)
    assert ok.action is Action.AUTO_ALLOW
    # …but a system-critical binary is escalated to CONFIRM by the authority floor
    base = PolicyDecision(Action.AUTO_ALLOW, "ordinary", RiskLevel.R1)
    gated = await loop._authority_floor(exec_tool, {"command": "mkfs /dev/sdb"}, base)
    assert gated.action is Action.CONFIRM and gated.risk is RiskLevel.R4
    assert "system-critical" in gated.reason


async def test_floor_never_loosens_an_already_gated_decision(live_pool: Any) -> None:
    loop = _make_loop(live_pool)
    exec_tool = loop.registry.get("execute_command")
    denied = PolicyDecision(Action.DENY, "denylisted", RiskLevel.R2)
    out = await loop._authority_floor(exec_tool, {"command": "ls"}, denied)
    assert out.action is Action.DENY  # only tightens auto-allow; never relaxes deny/confirm


# ---- LLM interpretation (model may only escalate) --------------------------------------------

async def test_model_may_escalate_an_unknown_tool() -> None:
    provider = FakeModelProvider(responses=[ChatResult("system_critical", None, [], 1, 1, "fake")])
    tier, rationale = await interpret_authority(provider, "obscure-wiper")
    assert tier is ExecAuthority.SYSTEM_CRITICAL and "raised" in rationale


async def test_model_cannot_weaken_the_deterministic_floor() -> None:
    # the model wrongly calls mkfs "normal" — the deterministic system_critical floor stands (§35)
    provider = FakeModelProvider(responses=[ChatResult("normal", None, [], 1, 1, "fake")])
    tier, _ = await interpret_authority(provider, "mkfs")
    assert tier is ExecAuthority.SYSTEM_CRITICAL


async def test_interpretation_falls_back_deterministically_on_garbage() -> None:
    provider = FakeModelProvider(responses=[ChatResult("¯\\_(ツ)_/¯", None, [], 1, 1, "fake")])
    tier, _ = await interpret_authority(provider, "apt")
    assert tier is ExecAuthority.ELEVATED  # unparseable → deterministic result


def _make_loop(pool: Any) -> Any:
    from sali.context.engine import ContextEngine
    from sali.retrieval.service import RetrievalService
    from sali.runtime.loop import AgentLoop
    from sali.security.confirm import AutoAllowConfirmer
    from sali.security.policy import PolicyEngine
    from sali.tools.registry import default_registry

    fake = FakeModelProvider()
    return AgentLoop(
        pool=pool, provider=fake, retrieval=RetrievalService(pool, fake),
        context=ContextEngine(fake, ctx_tokens=2048), registry=default_registry(),
        policy=PolicyEngine(), confirmer=AutoAllowConfirmer(), settings=_settings())


def _settings() -> Any:
    from sali.config.settings import Settings

    return Settings()
