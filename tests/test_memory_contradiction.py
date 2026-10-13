"""Functional-claim contradiction in memory (the memory analogue of graph set_fact)."""

from __future__ import annotations

from typing import Any

import pytest

from sali.core.enums import MemoryLayer, MemorySource
from sali.memory import writer

pytestmark = pytest.mark.db

CLAIM = "gpu|driver_version"


async def test_functional_claim_new_wins(db_conn: Any) -> None:
    old = await writer.remember(
        db_conn, layer=MemoryLayer.SYSTEM_ENV, content="GPU driver is 550",
        source=MemorySource.INFERENCE, functional=True, claim_key=CLAIM,
    )
    new = await writer.remember(
        db_conn, layer=MemoryLayer.SYSTEM_ENV, content="GPU driver is 610",
        source=MemorySource.SYSTEM_OBSERVATION, functional=True, claim_key=CLAIM,
    )
    assert new.id != old.id
    # exactly one current row for the claim, and it is the new value (fix M3)
    current = await db_conn.fetch(
        "SELECT content FROM memory WHERE claim_key=$1 AND valid_until IS NULL AND superseded_by IS NULL",
        CLAIM,
    )
    assert [r["content"] for r in current] == ["GPU driver is 610"]
    # old value is preserved as history, not deleted (rule 7)
    old_row = await db_conn.fetchrow("SELECT valid_until, superseded_by FROM memory WHERE id=$1", old.id)
    assert old_row["valid_until"] is not None and old_row["superseded_by"] == new.id
    assert await db_conn.fetchval(
        "SELECT count(*) FROM contradiction WHERE subject_type='memory' AND resolution='new_wins'"
    ) == 1


async def test_functional_claim_old_wins(db_conn: Any) -> None:
    strong = await writer.remember(
        db_conn, layer=MemoryLayer.SYSTEM_ENV, content="driver is 610",
        source=MemorySource.SYSTEM_OBSERVATION, functional=True, claim_key="gpu|d2",
    )
    await writer.remember(
        db_conn, layer=MemoryLayer.SYSTEM_ENV, content="driver is 550",
        source=MemorySource.INFERENCE, functional=True, claim_key="gpu|d2",
    )
    current = await db_conn.fetch(
        "SELECT content FROM memory WHERE claim_key='gpu|d2' AND valid_until IS NULL "
        "AND superseded_by IS NULL",
    )
    assert [r["content"] for r in current] == ["driver is 610"]  # weaker claim didn't win
    assert current  # exactly the one strong row remains current
    assert strong.id == (await db_conn.fetchval(
        "SELECT id FROM memory WHERE claim_key='gpu|d2' AND valid_until IS NULL"
    ))


async def test_functional_claim_same_value_corroborates(db_conn: Any) -> None:
    first = await writer.remember(
        db_conn, layer=MemoryLayer.SEMANTIC, content="uses postgres",
        source=MemorySource.SYSTEM_OBSERVATION, functional=True, claim_key="sali|datastore",
    )
    again = await writer.remember(
        db_conn, layer=MemoryLayer.SEMANTIC, content="uses postgres",
        source=MemorySource.SYSTEM_OBSERVATION, functional=True, claim_key="sali|datastore",
    )
    assert again.id == first.id
    # §6: same source re-stating the same functional value is tracked (evidence_count bumps) and holds
    # the claim current, but does NOT inflate confidence — self-restatement is not independent evidence.
    assert again.evidence_count == 2 and again.confidence == first.confidence
