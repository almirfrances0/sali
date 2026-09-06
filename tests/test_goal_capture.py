# tests/test_goal_capture.py
# Pin the KEEP-shape-equivalent (GOAL: / NONE) contract, the gate, the idempotency check,
# and the end-of-turn write. These are the six things that determine whether goals become
# first-class as Almir talks - if any of them regress, goals stop landing (or start landing
# as noise), and the agenda synthesiser silently degrades.

import os, uuid, asyncio, pytest
from types import SimpleNamespace

from sali.runtime.loop import (
    _goal_signal, _GOAL_RE, _keywords, _GOAL_PROMPT,
)


# ── the gate ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("txt", [
    "goal: deploy Salieno by end of Q1",
    "our goal is to migrate off Digital Ocean",
    "my goal is to write my first novel this year",
    "the goal is to reach 1000 paying customers",
    "let's build a metrics dashboard for the ingest pipeline",
    "let's deploy the new backend to Kali",
    "i want us to launch the app on TestFlight next month",
    "we should build a self-hosted email server",
    "add this as a goal: hire two engineers",
    "add that to goals",
    "make this a goal",
    "long-term I want to speak fluent Swahili",
    "we're building a homelab in the office",
    "we're deploying Salieno as our first real customer",
])
def test_gate_matches_real_goal_statements(txt):
    assert _goal_signal(txt), f"expected {txt!r} to match _GOAL_RE"


@pytest.mark.parametrize("txt", [
    "run the tests",                              # a request, not a goal
    "restart the daemon",                          # a request
    "what is the state of the agenda",             # a question
    "check the logs",                              # a request
    "sali, remember my birthday is march 12",      # a preference, handled by _capture_durable
    "i work from tanzania",                        # environment, handled by _capture_durable
    "you can email me at foo",                     # contact, handled by _capture_durable
    "generate a script that archives my documents",# a task, not a goal
    "i want a coffee",                             # not a goal-shaped want
    "let's talk about the weather",                # not one of the goal verbs
])
def test_gate_ignores_non_goals(txt):
    assert not _goal_signal(txt), f"{txt!r} should NOT match _GOAL_RE"


# ── the keyword helper ────────────────────────────────────────────────────
def test_keywords_ignores_stopwords_and_goal_vocabulary():
    kws = _keywords("let's deploy Salieno to production")
    assert "salieno" in kws
    assert "production" in kws
    assert "deploy" not in kws
    assert "let" not in kws


def test_keywords_ignore_short_tokens():
    assert "of" not in _keywords("a b c of")
    assert "abc" in _keywords("abc def")


def test_keywords_overlap_flags_duplicates():
    # two goal statements about the same thing should overlap heavily
    a = _keywords("deploy salieno backend to kali production")
    b = _keywords("let's build the salieno backend on our kali box")
    intersection = a & b
    assert "salieno" in intersection
    assert "backend" in intersection
    assert "kali" in intersection


def test_keywords_low_overlap_for_distinct_goals():
    a = _keywords("write a novel about tanzania")
    b = _keywords("deploy salieno backend to kali production")
    assert not (a & b)


# ── the prompt shape ──────────────────────────────────────────────────────
def test_prompt_asks_for_two_shape_output():
    p = _GOAL_PROMPT.format(msg="test")
    assert "GOAL:" in p
    assert "NONE" in p
    assert "one line" in p.lower() or "ONE line" in p


# ── the write path (integration - shape only, no DB) ──────────────────────
class _FakePool:
    async def acquire(self):
        raise RuntimeError("should not be called in this test")


class _FakeProvider:
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0
    async def chat(self, messages):
        self.calls += 1
        return SimpleNamespace(content=self.reply)


class _FakeJournal:
    def __init__(self):
        self.events = []
    async def event(self, name, payload):
        self.events.append((name, payload))


class _FakePub:
    async def publish(self, *a, **kw): pass


class _StubStore:
    """Stands in for GoalStore to observe the write without needing Postgres."""
    created = []
    def __init__(self, pool, pub):
        pass
    async def active(self, limit=50):
        return list(_StubStore._active)
    async def create(self, *, objective, origin="user"):
        gid = uuid.uuid4()
        _StubStore.created.append((objective, origin))
        return gid


@pytest.mark.asyncio
async def test_gate_skip_never_calls_the_model(monkeypatch):
    from sali.runtime import loop as _loop
    monkeypatch.setattr("sali.tasks.goals.GoalStore", _StubStore)
    _StubStore.created = []
    _StubStore._active = []
    prov = _FakeProvider("GOAL: unused")
    self = SimpleNamespace(provider=prov, pool=_FakePool(),
                           _publisher=_FakePub(),
                           _capture_goal=_loop.AgentLoop._capture_goal)
    await self._capture_goal(self, "restart the daemon", _FakeJournal())
    assert prov.calls == 0
    assert _StubStore.created == []


@pytest.mark.asyncio
async def test_goal_shape_lands_a_row(monkeypatch):
    from sali.runtime import loop as _loop
    monkeypatch.setattr("sali.tasks.goals.GoalStore", _StubStore)
    _StubStore.created = []
    _StubStore._active = []
    prov = _FakeProvider("GOAL: deploy Salieno on Kali")
    j = _FakeJournal()
    self = SimpleNamespace(provider=prov, pool=_FakePool(),
                           _publisher=_FakePub(),
                           _capture_goal=_loop.AgentLoop._capture_goal)
    await self._capture_goal(self, "let's deploy Salieno on Kali", j)
    assert _StubStore.created == [("deploy Salieno on Kali", "user")]
    assert j.events and j.events[0][0] == "goal_captured"


@pytest.mark.asyncio
async def test_none_reply_writes_nothing(monkeypatch):
    from sali.runtime import loop as _loop
    monkeypatch.setattr("sali.tasks.goals.GoalStore", _StubStore)
    _StubStore.created = []
    _StubStore._active = []
    prov = _FakeProvider("NONE")
    self = SimpleNamespace(provider=prov, pool=_FakePool(),
                           _publisher=_FakePub(),
                           _capture_goal=_loop.AgentLoop._capture_goal)
    await self._capture_goal(self, "let's build a metrics dashboard", _FakeJournal())
    assert _StubStore.created == []


@pytest.mark.asyncio
async def test_idempotent_when_similar_goal_active(monkeypatch):
    from sali.runtime import loop as _loop
    monkeypatch.setattr("sali.tasks.goals.GoalStore", _StubStore)
    _StubStore.created = []
    _StubStore._active = [
        {"objective": "deploy salieno on kali production"},
    ]
    prov = _FakeProvider("GOAL: let's deploy Salieno on the Kali box")
    self = SimpleNamespace(provider=prov, pool=_FakePool(),
                           _publisher=_FakePub(),
                           _capture_goal=_loop.AgentLoop._capture_goal)
    await self._capture_goal(self, "let's deploy Salieno on kali", _FakeJournal())
    assert _StubStore.created == []   # existing goal is close enough - no duplicate


@pytest.mark.asyncio
async def test_distinct_goal_lands_even_when_others_active(monkeypatch):
    from sali.runtime import loop as _loop
    monkeypatch.setattr("sali.tasks.goals.GoalStore", _StubStore)
    _StubStore.created = []
    _StubStore._active = [
        {"objective": "deploy salieno on kali production"},
    ]
    prov = _FakeProvider("GOAL: write a novel about tanzania")
    self = SimpleNamespace(provider=prov, pool=_FakePool(),
                           _publisher=_FakePub(),
                           _capture_goal=_loop.AgentLoop._capture_goal)
    await self._capture_goal(self, "long-term i want to write a novel about tanzania", _FakeJournal())
    assert _StubStore.created == [("write a novel about tanzania", "user")]


@pytest.mark.asyncio
async def test_leading_prose_before_goal_line_is_tolerated(monkeypatch):
    """Q2_K sometimes adds a preamble before the KEEP/GOAL line - accept the first GOAL: line."""
    from sali.runtime import loop as _loop
    monkeypatch.setattr("sali.tasks.goals.GoalStore", _StubStore)
    _StubStore.created = []
    _StubStore._active = []
    prov = _FakeProvider(
        "sure, here is the objective:\n\nGOAL: build a metrics dashboard\n\nlet me know if wrong.")
    self = SimpleNamespace(provider=prov, pool=_FakePool(),
                           _publisher=_FakePub(),
                           _capture_goal=_loop.AgentLoop._capture_goal)
    await self._capture_goal(self, "let's build a metrics dashboard", _FakeJournal())
    assert _StubStore.created == [("build a metrics dashboard", "user")]


@pytest.mark.asyncio
async def test_too_short_objective_is_refused(monkeypatch):
    from sali.runtime import loop as _loop
    monkeypatch.setattr("sali.tasks.goals.GoalStore", _StubStore)
    _StubStore.created = []
    _StubStore._active = []
    prov = _FakeProvider("GOAL: hi")   # 2 chars - well under the 8-char floor
    self = SimpleNamespace(provider=prov, pool=_FakePool(),
                           _publisher=_FakePub(),
                           _capture_goal=_loop.AgentLoop._capture_goal)
    await self._capture_goal(self, "goal: ", _FakeJournal())
    assert _StubStore.created == []
