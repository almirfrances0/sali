"""The host must never load sali:latest twice, and Sali is a single executive agent (user directive).

Two invariants pinned here as regression protection:
  1. The GPU inference lease serialises EVERY model generation — two never overlap, so the heavy model
     is never loaded a second time concurrently. (Combined with OLLAMA_NUM_PARALLEL=1 +
     OLLAMA_MAX_LOADED_MODELS=2 + the 140W power cap, active in the systemd override, sali:latest loads
     exactly once and the machine can't be tripped into a power-off.)
  2. Every chat generation uses ONE consistent context window (num_ctx), so Ollama keeps a single
     sali:latest instance rather than a second variant for a different option set.
  3. There is no subagent — the delegate tool is not even registered.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sali.config.settings import ModelSettings
from sali.provider.ollama import _gpu_lease
from sali.provider.presets import CREATIVE, DETERMINISTIC


async def test_gpu_lease_never_runs_two_generations_concurrently() -> None:
    # Two coroutines that each hold the inference lease briefly must NEVER overlap — the lease guarantees
    # only one generation at a time, so sali:latest is never loaded a second time concurrently.
    state = {"concurrent": 0, "max": 0}

    async def generation() -> None:
        async with _gpu_lease():
            state["concurrent"] += 1
            state["max"] = max(state["max"], state["concurrent"])
            await asyncio.sleep(0.02)
            state["concurrent"] -= 1

    await asyncio.gather(*[generation() for _ in range(5)])
    assert state["max"] == 1   # never two generations at once → never a second model load


def test_every_chat_generation_uses_one_consistent_context() -> None:
    from sali.provider.ollama import OllamaProvider

    provider = OllamaProvider(ModelSettings())
    msgs: list[Any] = []
    _, _, opts_a = provider._build(msgs, None, None, DETERMINISTIC)
    _, _, opts_b = provider._build(msgs, None, None, CREATIVE)
    # different sampling presets, but the SAME num_ctx → Ollama keeps one sali:latest instance
    assert opts_a["num_ctx"] == opts_b["num_ctx"]
    assert opts_a["num_ctx"] == min(provider.s.ctx_default, provider.s.ctx_max)


def test_no_subagent_delegate_tool_is_unregistered() -> None:
    from sali.tools.registry import default_registry

    assert "delegate" not in {t.name for t in default_registry().advertise()}   # single executive agent
