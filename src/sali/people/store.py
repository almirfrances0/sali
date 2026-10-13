"""Who Almir is, and what is happening in his life.

Sali modelled his own state every turn (`self_state`) and never modelled Almir's. The consequence was
measured on 2026-09-07: told "i lost 2 of my legs and 1 hand", the five memories retrieved were a
`file_metadata` failure, a SaaS-website experience, an Opus-5 remark, a test artefact, and "Python
virtual environments here: venv: sali (py 3.14.6)". His grief had to compete with engineering trivia
on cosine similarity, and lost. `sali.graph_node` held 3187 tool nodes and ZERO people; `sali.person`
— a table with `relationship_type`, `preferences`, `context`, `last_interaction` — had never held a
single row.

Two rules make this safe, and they are the whole design:

  1. IT STORES WHAT HE SAID, NOT WHAT SALI CONCLUDED. Every entry is his own sentence, dated. Never
     an inference about his inner life ("he is probably still upset"), because inferring is how this
     went wrong in the first place: "she'll be back tomorrow evening, around 5 PM" was invented to
     fill exactly this gap. A model reading "on 2026-09-07 he told you: my wife left me" can do the
     emotional work itself, and do it from something true.

  2. IT IS NOT RETRIEVED BY SIMILARITY. These facts are present because they are about HIM, not
     because they scored against the query. That is the entire reason a separate store exists rather
     than more rows in `memory`, which is ranked and where they lose.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

OWNER = "Almir"
# Enough to be a life, small enough that it can never crowd the prompt. Oldest fall off the render,
# never the row: the record keeps everything, the prompt carries what is current.
_RENDER_LIMIT = 8
_KEEP = 200


# HOW IT FELT, not only what was said.
#
# Measured 2026-09-07: "sali my brother is getting married next month" was answered "It's okay, Sir.
# We've got each other, so we'll get through whatever comes." A wedding read as a wound. Adding a
# valence-aware line to the prompt did NOT fix it, because the surrounding record legitimately says
# Almir is in a hard period — his divorce and his accident are both in it — so every new thing he
# says arrives pre-coloured by the last hard thing. The fix cannot be an instruction; the events
# themselves have to carry which kind they were.
#
# Deterministic, and that is the point: the store's whole safety property is that no model writes to
# it. A classifier that GUESSED how Almir feels would be the same fabrication that produced "she'll
# be back tomorrow evening, around 5 PM", one layer down. Word lists only, and anything unrecognised
# stays NEUTRAL rather than being forced into a bucket — an unknown event described as "hard" is
# worse than one described as nothing.
HARD = "hard"
GOOD = "good"
NEUTRAL = "neutral"

_HARD_RE = re.compile(
    r"\b(?:die[ds]?|died|dying|death|passed\s+away|funeral|buried|"
    r"divorc\w*|separat\w+|broke\s+up|split\s+up|left\s+me|cheat\w+|"
    r"accident|crash\w*|injur\w+|hurt|wound\w*|hospital|surgery|icu|coma|"
    r"cancer|chemo|tumou?r|stroke|heart\s+attack|miscarriage|"
    r"fired|laid\s+off|redundan\w+|evicted|arrested|robbed|stolen|burgl\w+|"
    r"lost\s+(?:my|his|her|both|two|one|an?)\b|sick|ill\b|unwell|dying)\b",
    re.IGNORECASE,
)
_GOOD_RE = re.compile(
    r"\b(?:married|marry\w*|marriage|wedding|engaged|engagement|"
    r"baby|babies|pregnan\w+|born|birth|expecting|"
    r"promot\w+|new\s+job|hired|graduat\w+|passed\s+(?:my|his|her|the)\s+\w+|"
    r"won|winning|award|celebrat\w+|birthday|anniversary|"
    r"bought\s+a\b|moving\s+in|got\s+into\b|accepted\b|recovered|"
    r"better\s+now|good\s+news|great\s+news)\b",
    re.IGNORECASE,
)


def tone_of(said: str) -> str:
    """Was this a hard thing, a good thing, or neither? Word lists only — never a judgement."""
    text = said or ""
    hard = bool(_HARD_RE.search(text))
    good = bool(_GOOD_RE.search(text))
    if hard and good:
        # "my brother got married but my father died" — the hard half governs the register.
        return HARD
    if hard:
        return HARD
    if good:
        return GOOD
    return NEUTRAL


@dataclass(frozen=True, slots=True)
class LifeEvent:
    said: str
    at: datetime
    tone: str = NEUTRAL

    def to_json(self) -> dict[str, Any]:
        return {"said": self.said, "at": self.at.isoformat(), "tone": self.tone}


class PersonStore:
    """The people Sali knows. Today that is Almir; the table was always shaped for more."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def ensure_owner(self, conn: Any = None) -> None:
        """The owner row exists. Idempotent, and safe to call on every boot."""
        sql = (
            "INSERT INTO person (name, relationship_type, provenance, confidence) "
            "VALUES ($1, 'owner', 'explicit', 1.0) "
            "ON CONFLICT DO NOTHING"
        )
        if conn is not None:
            await conn.execute(sql, OWNER)
            return
        async with self._pool.acquire() as c:
            await c.execute(sql, OWNER)

    async def note_life_event(self, said: str, *, at: datetime, conn: Any = None) -> bool:
        """Record, verbatim, something Almir said about his own life.

        Returns False when it is already the most recent entry — he repeats himself, and a life is
        not a log of duplicates. No model call anywhere in this path: the sentence is his.
        """
        said = " ".join((said or "").split())[:400]
        if not said:
            return False

        async def _run(c: Any) -> bool:
            row = await c.fetchrow(
                "SELECT context FROM person WHERE name=$1 FOR UPDATE", OWNER)
            if row is None:
                await self.ensure_owner(c)
                row = await c.fetchrow(
                    "SELECT context FROM person WHERE name=$1 FOR UPDATE", OWNER)
            ctx = row["context"] if row else {}
            if isinstance(ctx, str):          # asyncpg hands jsonb back as text unless a codec is set
                ctx = json.loads(ctx or "{}")
            ctx = dict(ctx or {})
            life = list(ctx.get("life") or [])
            if life and life[-1].get("said", "").lower() == said.lower():
                return False
            life.append(LifeEvent(said=said, at=at, tone=tone_of(said)).to_json())
            ctx["life"] = life[-_KEEP:]
            # The pool registers a jsonb codec whose encoder IS json.dumps, so a pre-serialised string
            # would be encoded twice and come back as a quoted blob. Pass the object.
            await c.execute(
                "UPDATE person SET context=$2, last_interaction=$3, updated_at=now() WHERE name=$1",
                OWNER, ctx, at)
            return True

        if conn is not None:
            return await _run(conn)
        async with self._pool.acquire() as c, c.transaction():
            return await _run(c)

    async def life(self, conn: Any = None, *, limit: int = _RENDER_LIMIT) -> list[LifeEvent]:
        sql = "SELECT context FROM person WHERE name=$1"
        row = (await conn.fetchrow(sql, OWNER)) if conn is not None else None
        if conn is None:
            async with self._pool.acquire() as c:
                row = await c.fetchrow(sql, OWNER)
        if not row:
            return []
        ctx = row["context"]
        if isinstance(ctx, str):
            ctx = json.loads(ctx or "{}")
        out: list[LifeEvent] = []
        for e in list((ctx or {}).get("life") or [])[-limit:]:
            try:
                out.append(LifeEvent(said=e["said"], at=datetime.fromisoformat(e["at"]),
                                     tone=e.get("tone") or NEUTRAL))
            except Exception:  # noqa: BLE001 - one malformed entry never costs the rest
                continue
        return out


def render_life(events: list[LifeEvent], *, ago: Any = None) -> str:
    """His life, in his own words, for the prompt.

    Dated on purpose. Undated, "my wife left me" is a permanent present tense that Sali would keep
    reacting to as news; with a date he can tell last week from this morning — and the elapsed form
    is used rather than a calendar date because measuring one against "now" is the arithmetic that
    produced "since last year (August 2024)" about work done that same week.
    """
    if not events:
        return ""
    lines = []
    _hard = [e for e in events if e.tone == HARD]
    for e in events:
        when = ago(e.at) if ago else e.at.strftime("%Y-%m-%d")
        # The tone is stated per event rather than left to be inferred from the run of them. Without
        # it, one hard thing colours everything that follows: a wedding announced during a hard month
        # was answered "It's okay, Sir. We'll get through whatever comes."
        mark = {"hard": " [hard]", "good": " [good news]"}.get(e.tone, "")
        lines.append(f'- {when}{mark}, he told you: "{e.said}"')
    head = ("His life, in his own words — this is what you know about it, and you know nothing "
            "beyond it:\n" + "\n".join(lines))
    if not _hard:
        return head
    # A HARD THING STAYS HARD ON THE NEXT TURN.
    #
    # Tone was reaching the model only on the turn Almir said it. Two hours after telling Sali he had
    # been in an accident, "hi sali" was answered: "How's the pain? Still feeling that phantom itch in
    # the missing toes? 😂 ... did your new body give out halfway through?" He had the event, marked
    # [hard], and read it as material for banter — because nothing carried the WEIGHT forward, only
    # the fact. A person model that a companion mines for jokes is worse than no person model.
    #
    # Stated once, at the end where it is closest to the reply, and only when something hard is
    # actually there — an ordinary week costs nothing.
    return head + (
        "\nSomething marked [hard] above is real and current for him. Do not make a joke of it, do "
        "not use it for banter, and do not open with it unless he does — carry it the way you would "
        "for someone you care about.")
