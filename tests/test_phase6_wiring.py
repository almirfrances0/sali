"""PHASE 6 — reliability matrix · end-to-end RESPOND-path wiring.

The unit tiers prove the detectors are correct; this tier proves the LOOP actually invokes response
grounding on the settled reply and ships the honest text — the keystone that was firing in production
(4 real strikes) but had no permanent end-to-end lock. Drives the real AgentLoop with a scripted model
over the live test pool.
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.config.settings import DbSettings, ModelSettings, Settings
from sali.context.engine import ContextEngine
from sali.core.ids import new_id
from sali.provider.base import ChatResult
from sali.provider.fake import FakeModelProvider
from sali.retrieval.service import RetrievalService
from sali.runtime.loop import AgentLoop
from sali.security.confirm import AutoAllowConfirmer
from sali.security.policy import PolicyEngine
from sali.tools.registry import default_registry

pytestmark = pytest.mark.db


def _settings() -> Settings:
    return Settings(db=DbSettings(name="sali_test"), model=ModelSettings(provider="fake"))


def _loop(pool: Any, provider: FakeModelProvider) -> AgentLoop:
    return AgentLoop(
        pool=pool, provider=provider,
        retrieval=RetrievalService(pool, provider),
        context=ContextEngine(provider, ctx_tokens=8192),
        registry=default_registry(), policy=PolicyEngine(),
        confirmer=AutoAllowConfirmer(), settings=_settings(),
    )


async def test_fabricated_action_is_struck_end_to_end(live_pool: Any) -> None:
    # The model claims an effectful action but ran ZERO tools — the RESPOND-path grounding must strike
    # the fabrication before the reply ships, so "restarted nginx" never reaches Almir.
    fake = FakeModelProvider(responses=[
        ChatResult("I restarted nginx and cleared the cache for you.", None, [], 6, 5, "fake"),
    ])
    result = await _loop(live_pool, fake).run("did you restart nginx?", session_id=new_id())
    assert "restarted nginx" not in result.text.lower()
    assert "cleared the cache" not in result.text.lower()


async def test_truthful_nonclaim_reply_ships_untouched(live_pool: Any) -> None:
    # A reply that asserts no operational claim must be shipped verbatim — grounding never touches a
    # true/neutral reply (precision-first).
    fake = FakeModelProvider(responses=[
        ChatResult("Sure — what would you like me to check first?", None, [], 4, 4, "fake"),
    ])
    result = await _loop(live_pool, fake).run("help me out", session_id=new_id())
    assert "check first" in result.text.lower()
