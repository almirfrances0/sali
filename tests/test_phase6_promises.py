"""PHASE 6 — reliability matrix · proactive-outreach gate (events/communication_decision.py).

The "would silence be better?" gate decides whether a self-initiated reminder ships. The harden pass
found two ways it failed: suppression was keyed on KIND alone (so 5 ignored reminders about one thing
silenced the ENTIRE channel forever), and reminders ignored the user-focus signal (fired mid-chat).
This locks both fixes ([H]) with a mocked pool — deterministic, no DB, no daemon.
"""

from __future__ import annotations

from typing import Any

from sali.events.communication_decision import CommunicationDecisionEngine


def _pool(ignored_subject: str | None = None) -> Any:
    """A pool whose chronically-ignored query returns 5 'none' rows ONLY when its params mention
    `ignored_subject`; every frequency count is 0 and every write is a no-op."""
    class _Conn:
        async def fetchval(self, *a: Any, **k: Any) -> int: return 0
        async def fetch(self, q: str, *a: Any, **k: Any) -> list[dict[str, str]]:
            if ignored_subject is not None and any(p == ignored_subject for p in a):
                return [{"engagement": "none"}] * 5
            return []
        async def execute(self, *a: Any, **k: Any) -> None: return None
    class _Acq:
        async def __aenter__(self) -> Any: return _Conn()
        async def __aexit__(self, *a: Any) -> bool: return False
    class _Pool:
        def acquire(self) -> Any: return _Acq()
        async def fetchval(self, *a: Any, **k: Any) -> int: return 0
        async def fetch(self, q: str, *a: Any, **k: Any) -> list[dict[str, str]]:
            if ignored_subject is not None and any(p == ignored_subject for p in a):
                return [{"engagement": "none"}] * 5
            return []
        async def execute(self, *a: Any, **k: Any) -> None: return None
    return _Pool()


# ---- reminder channel no longer deadlocks (per-subject learning) ---------------------------------

async def test_ignored_subject_A_does_not_silence_a_different_subject_B() -> None:  # [H]
    eng = CommunicationDecisionEngine(_pool(ignored_subject="commit-A"), None)
    d = await eng.evaluate(kind="reminder", subject_ref="commit-B", message="Heads up about B",
                           relevance=0.9, user_focused=False)
    assert d.send is True and "kind_learned_bad" not in d.reason_codes


async def test_chronically_ignored_subject_is_still_suppressed() -> None:
    eng = CommunicationDecisionEngine(_pool(ignored_subject="commit-A"), None)
    d = await eng.evaluate(kind="reminder", subject_ref="commit-A", message="Heads up about A",
                           relevance=0.9, user_focused=False)
    assert d.send is False and "kind_learned_bad" in d.reason_codes


# ---- reminders defer to focused work; a concern still gets through -------------------------------

async def test_reminder_deferred_while_user_focused() -> None:  # [H]
    eng = CommunicationDecisionEngine(_pool(), None)
    d = await eng.evaluate(kind="reminder", subject_ref="c1", message="x",
                           relevance=0.8, user_focused=True)
    assert d.send is False and "user_focused" in d.reason_codes


async def test_concern_bypasses_user_focus() -> None:
    eng = CommunicationDecisionEngine(_pool(), None)
    d = await eng.evaluate(kind="concern", subject_ref="c2", message="x",
                           relevance=0.9, user_focused=True)
    assert d.send is True
