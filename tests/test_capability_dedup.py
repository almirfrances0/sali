"""Anti-relearn: two semantically identical objectives must not create two capability rows.

Almir's requirement (Prompt 8 §7): "Sali should not repeatedly rediscover the same capability." The
naming model produces natural short names, and two objectives about the same work can yield different
words - live proof from the database before this fix: three separate rows for the same skill,
`create python archive script`, `create python archive script` (again, different UUID) and `write
python archive script`, each with its own use_count that never accumulated on one.

Two layers:
  * A normaliser strips the leading verb (create/write/build/generate/make/produce/set/configure/
    install/add/compose/assemble/prepare) and stopwords. Its output is compared exactly.
  * observe() consults it at insert time: if any existing capability in the same scope has the same
    normalised form, THAT row is reused - evidence accrues on one skill instead of scattering across
    near-duplicates.
"""

from __future__ import annotations

from typing import Any

import pytest

from sali.learning.capability import _normalise_name

pytestmark = pytest.mark.db


class _Pool:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def acquire(self) -> Any:
        conn = self._conn

        class _Ctx:
            async def __aenter__(self) -> Any: return conn
            async def __aexit__(self, *exc: Any) -> bool: return False

        return _Ctx()


# ── the normaliser is what makes it correct ─────────────────────────────────────────────────────

@pytest.mark.parametrize("a,b", [
    ("create python archive script", "write python archive script"),
    ("write python archive script", "create a python archive script"),
    ("build a static tailwind site", "generate a static tailwind site"),
    ("configure an nginx vhost", "set up an nginx vhost"),
    ("install postgres locally", "add postgres locally"),
])
def test_synonymous_verb_forms_normalise_the_same(a: str, b: str) -> None:
    """Real dups from live data. If the normaliser makes these different, `observe()` will keep
    minting new rows for the same skill - the exact failure mode this fixes."""
    assert _normalise_name(a) == _normalise_name(b), f"{a!r} vs {b!r}"


@pytest.mark.parametrize("a,b", [
    ("configure an nginx vhost", "configure an apache vhost"),  # different tool
    ("write python archive script", "write python restore script"),  # opposite verb of work
    ("build a static site", "build a docker image"),  # different subject entirely
])
def test_genuinely_different_skills_stay_different(a: str, b: str) -> None:
    """The false-merge direction is the destructive one: two distinct skills collapsed cannot be
    undone from data and would attribute one skill's failures to another. The normaliser must not
    over-simplify to the point of merging real distinctions."""
    assert _normalise_name(a) != _normalise_name(b), f"{a!r} vs {b!r}"


# ── observe() reuses the existing row when a fuzzy match is found ────────────────────────────────

async def test_a_second_observe_of_a_synonymous_skill_reuses_the_same_row(db_conn: Any) -> None:
    """The live-data behaviour that produced the duplication: today the DB has three rows for the
    same skill because each observe minted a new UUID. After this fix, the second observe returns
    the FIRST row's id."""
    from sali.learning.capability import CapabilityStore

    store = CapabilityStore(_Pool(db_conn))
    first = await store.observe(name="create python archive script", status="verified",
                                scope_ref="local")
    second = await store.observe(name="write python archive script", status="successful",
                                 scope_ref="local")
    third = await store.observe(name="build a python archive script", status="attempted",
                                scope_ref="local")

    assert first == second == third, "one skill, one row - three observes must not create three rows"

    count = await db_conn.fetchval(
        "SELECT count(*) FROM capability WHERE scope='environment' AND coalesce(scope_ref,'')='local'")
    assert count == 1, "exactly one capability row"


async def test_the_kept_row_keeps_its_original_name(db_conn: Any) -> None:
    """The FIRST-observed name is the one that persists - re-labelling a merge would break every
    prior reference (memory entries, acquisitions, usage rows) that already point at the original
    name. Reuse is a merge onto the existing identity, not a rename."""
    from sali.learning.capability import CapabilityStore

    store = CapabilityStore(_Pool(db_conn))
    _ = await store.observe(name="create python archive script", status="verified",
                            scope_ref="local")
    _ = await store.observe(name="write python archive script", status="successful",
                            scope_ref="local")
    row = await db_conn.fetchrow(
        "SELECT name FROM capability WHERE coalesce(scope_ref,'')='local'")
    assert row is not None and row["name"] == "create python archive script"


async def test_a_genuinely_new_skill_still_gets_its_own_row(db_conn: Any) -> None:
    """The other half: dedup must not swallow distinct skills. A different subject, different work -
    two rows, both accumulating evidence on their own."""
    from sali.learning.capability import CapabilityStore

    store = CapabilityStore(_Pool(db_conn))
    _ = await store.observe(name="configure an nginx vhost", status="verified", scope_ref="local")
    _ = await store.observe(name="write python archive script", status="verified", scope_ref="local")
    count = await db_conn.fetchval(
        "SELECT count(*) FROM capability WHERE coalesce(scope_ref,'')='local'")
    assert count == 2


async def test_scope_isolates_dedup(db_conn: Any) -> None:
    """Two environments can legitimately have the same-named skill, and merging across scopes would
    conflate a capability verified in one place with an unverified one elsewhere. The dedup lookup
    must scope-filter."""
    from sali.learning.capability import CapabilityStore

    store = CapabilityStore(_Pool(db_conn))
    _ = await store.observe(name="create python archive script", status="verified",
                            scope_ref="host-a")
    _ = await store.observe(name="write python archive script", status="attempted",
                            scope_ref="host-b")
    count = await db_conn.fetchval("SELECT count(*) FROM capability")
    assert count == 2
