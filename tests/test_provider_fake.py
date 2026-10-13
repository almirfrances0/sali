from __future__ import annotations

import math

from sali.provider.base import ChatMessage, ChatResult, ModelProvider
from sali.provider.fake import FakeModelProvider


def test_fake_satisfies_protocol() -> None:
    assert isinstance(FakeModelProvider(), ModelProvider)


async def test_fake_chat_scripted() -> None:
    scripted = ChatResult(
        content="hi", thinking=None, tool_calls=[], tokens_in=1, tokens_out=1, model="fake"
    )
    p = FakeModelProvider(responses=[scripted])
    r = await p.chat([ChatMessage(role="user", content="hello")])
    assert r.content == "hi"
    assert len(p.calls) == 1


async def test_fake_embed_is_deterministic_unit_norm() -> None:
    p = FakeModelProvider(dim=768)
    a = await p.embed(["same text"])
    b = await p.embed(["same text"])
    assert a == b
    assert len(a[0]) == 768
    assert abs(math.sqrt(sum(x * x for x in a[0])) - 1.0) < 1e-6


def test_count_tokens_positive() -> None:
    assert FakeModelProvider().count_tokens("abcd" * 10) >= 1
