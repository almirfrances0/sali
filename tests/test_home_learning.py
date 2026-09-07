"""Sali learns his own machine, and keeps what he learned when the network is gone.

Three capabilities, one file:
  (A) "if today he can't run ping, tomorrow he knows"  — sali/learning/environment.py
  (C) "if there is no internet he can still use what he searched before" — the stale-when-offline
      path in sali/tools/builtins/web.py

The negative cases matter more than the positive ones here. A mechanism that turns failures into
"I can't do that" is one bad match away from learned helplessness, so most of these tests assert
that something is NOT recorded.
"""

from __future__ import annotations

import types

import httpx
import pytest

from sali.learning import environment as env
from sali.tools.builtins import web


# ── (A) the machine self-model ───────────────────────────────────────────────────────────────────

def test_a_missing_binary_is_learned_from_one_failure() -> None:
    """One unambiguous 'command not found' is a complete observation. It must not wait for three."""
    assert env.classify_constraint("ping", "/bin/sh: 1: ping: command not found") == env.MISSING
    assert env.classify_constraint("nmap", "sh: nmap: command not found") == env.MISSING
    assert env.classify_constraint("nmap", "zsh: command not found: nmap") == env.MISSING


def test_a_privilege_refusal_is_a_different_constraint() -> None:
    assert env.classify_constraint(
        "tcpdump", "tcpdump: eth0: You don't have permission to capture") == env.NEEDS_PRIVILEGE
    assert env.classify_constraint(
        "ping", "ping: socket: Operation not permitted") == env.NEEDS_PRIVILEGE


@pytest.mark.parametrize("error", [
    "ping: connection timed out",
    "ping: connect: Network is unreachable",
    "curl: (7) Failed to connect: Connection refused",
    "ping: invalid option -- 'z'",
    "usage: ping [-aAbBdDfhLnOqrRUvV64]",
])
def test_a_transient_and_usage_failures_are_never_constraints(error: str) -> None:
    """GUARD 1. The network being down, or a flag being wrong, says nothing about the machine.
    Recording these would teach Sali he cannot do things he can do."""
    assert env.classify_constraint("ping", error) is None
    assert env.classify_constraint("curl", error) is None


def test_a_an_error_about_the_argument_is_not_about_the_binary() -> None:
    """GUARD 2. `cat` is installed and works; the FILE is missing. Reading this as 'cat is not
    installed' is the exact over-learning this guard exists to stop."""
    assert env.classify_constraint("cat", "cat: notes.txt: No such file or directory") is None
    assert env.classify_constraint("cat", "cat: /etc/shadow: Permission denied") is None
    # ...while the SAME WORDS, said about the program itself, do register: when the shell reports
    # "No such file or directory" for the executable path, the executable really is absent.
    assert env.classify_constraint("foo", "bash: /usr/bin/foo: No such file or directory") == env.MISSING
    assert env.classify_constraint("/usr/bin/foo",
                                   "bash: /usr/bin/foo: No such file or directory") == env.MISSING


def test_a_an_error_that_never_names_the_binary_is_ignored() -> None:
    assert env.classify_constraint("ping", "something went wrong") is None
    assert env.classify_constraint("", "ping: command not found") is None


@pytest.mark.asyncio
async def test_a_learn_surface_reinforce_and_self_heal(live_pool) -> None:
    """The whole arc on a real database: learn it, show it to Sali, reinforce it, retire it."""
    async with live_pool.acquire() as conn:
        # Day one: it fails.
        assert await env.note_tool_outcome(
            conn, binary="ping", success=False,
            error_text="/bin/sh: 1: ping: command not found") == env.MISSING

        # Day two: he sees it BEFORE reaching for it again. This is the whole point.
        rows = await env.learned_constraints(conn)
        assert any(r["binary"] == "ping" for r in rows)
        block = env.render_constraints(rows)
        assert "ping" in block and "not installed here" in block

        # Seeing it again corroborates one claim; it does not pile up duplicates.
        assert await env.note_tool_outcome(
            conn, binary="ping", success=False,
            error_text="/bin/sh: 1: ping: command not found") == "reinforced"
        assert await conn.fetchval(
            "SELECT count(*) FROM memory WHERE claim_key='env:tool:ping' "
            "AND valid_until IS NULL") == 1

        # GUARD 3, the self-heal: someone installs it, it runs, the belief goes away by itself.
        assert await env.note_tool_outcome(conn, binary="ping", success=True) == "retired"
        assert not any(r["binary"] == "ping" for r in await env.learned_constraints(conn))
        # Closed, not deleted — the history of what Sali believed stays auditable.
        assert await conn.fetchval(
            "SELECT count(*) FROM memory WHERE claim_key='env:tool:ping' "
            "AND valid_until IS NOT NULL") == 1


@pytest.mark.asyncio
async def test_a_success_for_an_unconstrained_binary_writes_nothing(live_pool) -> None:
    async with live_pool.acquire() as conn:
        assert await env.note_tool_outcome(conn, binary="echo", success=True) is None
        assert await conn.fetchval(
            "SELECT count(*) FROM memory WHERE claim_key='env:tool:echo'") == 0


def test_a_nothing_learned_renders_nothing() -> None:
    """A heading with no content under it is worse than silence — this ships on an empty machine."""
    assert env.render_constraints([]) == ""


# ── (C) what he learned stays usable offline ─────────────────────────────────────────────────────

class _DeadNetwork:
    """Every backend unreachable — the phone-line-is-cut case."""

    def __init__(self, *a, **k) -> None: ...
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get(self, *a, **k): raise httpx.ConnectError("Network is unreachable")
    async def post(self, *a, **k): raise httpx.ConnectError("Network is unreachable")


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(web.httpx, "AsyncClient", _DeadNetwork)
    monkeypatch.setattr(web, "_SEARXNG_URL", "http://127.0.0.1:9/unreachable")


@pytest.mark.asyncio
async def test_c_a_past_search_is_still_usable_with_no_network(live_pool, offline) -> None:
    ctx = types.SimpleNamespace(pool=live_pool)
    target = "how to fix xdotool headless|6"
    payload = {"query": "how to fix xdotool headless", "retrieved_at": "2026-08-01T10:00:00+00:00",
               "backend": "searxng",
               "results": [{"title": "xdotool needs a display", "url": "https://wiki.archlinux.org/x",
                            "domain": "wiki.archlinux.org", "snippet": "set DISPLAY=:0"}]}
    await web._cache_put(ctx, "search", target, payload, 200)
    # Age it past the 24h TTL — the exact moment the old code threw the knowledge away.
    async with live_pool.acquire() as conn:
        await conn.execute("UPDATE sali.web_cache SET fetched_at = now() - interval '30 days'")

    # Online, the TTL still governs: a stale row is not served as fresh.
    assert await web._cache_get(ctx, "search", target) is None

    res = await web.WebSearch().run(
        {"query": "how to fix xdotool headless", "max_results": 6}, ctx)
    assert res.ok, "a search Sali has done before must still work offline"
    assert res.output["results"][0]["title"] == "xdotool needs a display"


@pytest.mark.asyncio
async def test_c_the_offline_answer_is_honest_about_being_old(live_pool, offline) -> None:
    """Serving old knowledge is right; passing it off as a live check is not."""
    ctx = types.SimpleNamespace(pool=live_pool)
    target = "kubernetes rollout stuck|6"
    await web._cache_put(ctx, "search", target,
                         {"query": "kubernetes rollout stuck", "backend": "searxng",
                          "retrieved_at": "2026-08-01T10:00:00+00:00", "results": [{"title": "t"}]}, 200)
    async with live_pool.acquire() as conn:
        await conn.execute("UPDATE sali.web_cache SET fetched_at = now() - interval '9 days'")

    res = await web.WebSearch().run({"query": "kubernetes rollout stuck", "max_results": 6}, ctx)
    assert res.output["stale"] is True
    assert "9 days ago" in res.output["offline_note"]
    assert "days ago" in res.display
    # The original retrieval time survives — nothing downstream can mistake this for a live read.
    assert res.output["retrieved_at"] == "2026-08-01T10:00:00+00:00"


@pytest.mark.asyncio
async def test_c_offline_with_nothing_cached_still_fails_honestly(live_pool, offline) -> None:
    """The fallback must never become a licence to invent. No copy → the real failure stands."""
    ctx = types.SimpleNamespace(pool=live_pool)
    res = await web.WebSearch().run({"query": "never searched before", "max_results": 6}, ctx)
    assert res.ok is False
    assert res.error and "failed" in res.error.lower()


@pytest.mark.asyncio
async def test_c_a_page_read_before_is_still_readable_offline(live_pool, offline) -> None:
    ctx = types.SimpleNamespace(pool=live_pool)
    url = "https://docs.python.org/3/library/asyncio.html"
    await web._cache_put(ctx, "fetch", url,
                         {"url": url, "domain": "docs.python.org", "title": "asyncio",
                          "retrieved_at": "2026-08-01T10:00:00+00:00", "status": 200,
                          "content": "# asyncio\\nrun() runs a coroutine", "links": []}, 200)
    async with live_pool.acquire() as conn:
        await conn.execute("UPDATE sali.web_cache SET fetched_at = now() - interval '40 days'")

    res = await web.WebFetch().run({"url": url}, ctx)
    assert res.ok and "run() runs a coroutine" in res.output["content"]
    assert res.output["stale"] is True
