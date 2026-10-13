"""Write-ahead run journal.

Each state transition and event is committed immediately (the journal connection runs in
autocommit), so a crash leaves a truthful trail in ``agent_runs``/``run_events`` to recover
from (engineering rule 14).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sali.core.ids import new_id
from sali.runtime.state import RunState


class RunJournal:
    def __init__(self, conn: Any, run_id: UUID, session_id: UUID) -> None:
        self.conn = conn
        self.run_id = run_id
        self.session_id = session_id
        self._seq = 0

    @classmethod
    async def start(
        cls, conn: Any, session_id: UUID, user_input: str, *, run_id: UUID | None = None,
        internal: bool = False,
    ) -> RunJournal:
        """Start a new run journal. If ``run_id`` is provided (from the execution coordinator),
        it is used as the canonical run ID — ensuring one run_id per turn across the entire system.

        ``internal`` records that this turn's input is something SALI gave himself, not something Almir
        said. It rides in the existing `snapshot` jsonb rather than a new column, and it matters after a
        crash: startup recovery re-drives an interrupted run from its stored `user_input`, and without
        this flag a background turn came back as a foreground one and its internal instructions were
        written into Almir's transcript as his own words."""
        rid = run_id or new_id()
        await conn.execute(
            "INSERT INTO agent_runs (run_id, session_id, user_input, state, status, snapshot) "
            "VALUES ($1,$2,$3,$4,'running',$5)",
            rid, session_id, user_input, RunState.INPUT.value,
            {"internal": True} if internal else {},
        )
        return cls(conn, rid, session_id)

    async def event(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        latency_ms: int | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
    ) -> None:
        self._seq += 1
        await self.conn.execute(
            "INSERT INTO run_events (run_id, seq, kind, payload, latency_ms, tokens_in, tokens_out) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7)",
            self.run_id, self._seq, kind, payload or {}, latency_ms, tokens_in, tokens_out,
        )

    async def set_state(self, state: RunState, *, iteration: int | None = None) -> None:
        if iteration is None:
            await self.conn.execute(
                "UPDATE agent_runs SET state=$1, updated_at=now() WHERE run_id=$2",
                state.value, self.run_id,
            )
        else:
            await self.conn.execute(
                "UPDATE agent_runs SET state=$1, iteration=$2, updated_at=now() WHERE run_id=$3",
                state.value, iteration, self.run_id,
            )

    async def finish(self, status: str) -> None:
        await self.conn.execute(
            "UPDATE agent_runs SET status=$1, updated_at=now() WHERE run_id=$2",
            status, self.run_id,
        )
