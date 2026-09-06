"""PHASE 6 — reliability matrix · epistemic humility.

verify/claims.py grounds Almir's INCOMING claims and rewrites a checkable question into a probe; the
dead-work containment in task_tool.py stops an internal turn from resurrecting a just-killed objective.
Both had the harden-pass 'up to date' collision and single-token blanket-block bugs. This locks the
deterministic pieces ([H] = harden regression). Pure functions + a mocked pool — no machine, no daemon.
"""

from __future__ import annotations

from typing import Any

from sali.tools.builtins.task_tool import _recently_dead_objective
from sali.verify.claims import _SERVICE_RE, _dequestion


# ---- 'up to date' no longer misread as service liveness ------------------------------------------

def test_up_to_date_question_not_misprobed() -> None:  # [H]
    # version-currency question must NOT become the liveness form 'docker is up'
    assert _dequestion("is docker up to date?") != "docker is up"
    # plain liveness still works
    assert _dequestion("is nginx up?") == "nginx is up"


def test_up_to_date_declarative_not_matched_as_service() -> None:  # [H]
    assert _SERVICE_RE.search("apparmor is up to date") is None
    assert _SERVICE_RE.search("the team is up and running") is None
    assert _SERVICE_RE.search("nginx is up") is not None  # plain liveness preserved


# ---- dead-work containment (mocked pool) ---------------------------------------------------------

def _pool(rows: list[dict[str, Any]]) -> Any:
    class _Conn:
        async def fetch(self, *a: Any, **k: Any) -> Any: return rows
        async def fetchrow(self, *a: Any, **k: Any) -> Any: return rows[0] if rows else None
    class _Acq:
        async def __aenter__(self) -> Any: return _Conn()
        async def __aexit__(self, *a: Any) -> bool: return False
    class _Pool:
        def acquire(self) -> Any: return _Acq()
        async def fetch(self, *a: Any, **k: Any) -> Any: return rows
    return _Pool()


async def test_dead_single_word_objective_no_longer_blanket_blocks() -> None:  # [H]
    p = _pool([{"objective": "check nginx", "status": "done"}])
    assert await _recently_dead_objective(p, "restart the nginx service now") is None


async def test_dead_single_word_owners_no_longer_blanket_blocks() -> None:  # [H]
    p = _pool([{"objective": "find owners", "status": "done"}])
    assert await _recently_dead_objective(p, "list all the owners of the company assets") is None


async def test_dead_unrelated_single_shared_token_not_blocked() -> None:  # [H]
    p = _pool([{"objective": "back up the database", "status": "done"}])
    assert await _recently_dead_objective(p, "connect to the database and run a query") is None


async def test_dead_reworded_respawn_caught_via_stemming() -> None:  # [H] owners->owner + tanzhost
    p = _pool([{"objective": "scrape tanzhost owners into spreadsheet", "status": "revoked"}])
    assert await _recently_dead_objective(p, "get me the tanzhost owner list") is not None


async def test_dead_true_respawn_still_caught() -> None:
    p = _pool([{"objective": "scrape tanzhost owners into a spreadsheet", "status": "revoked"}])
    assert await _recently_dead_objective(p, "scrape the tanzhost owners into a spreadsheet again") is not None
