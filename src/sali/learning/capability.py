"""CapabilityStore (§12-16/§32/§49) — what Sali has LEARNED to do, evidence-backed and environment-scoped.

A capability is not a hardcoded tool. It is know-how that Sali acquires and that gains confidence from
actual evidence: unknown → researched → attempted → successful → verified → available, or degrades /
deprecates. A failed attempt is useful negative knowledge, never a claimed capability (§14). Each
capability is scoped (verified on THIS environment ≠ works everywhere, §16/§49) and points at the
skills / procedures / experiences that support it — the bridge between memory, skills, and experience.

This builds the capability-growth LIFECYCLE so that, in future prompts, Sali can encounter something he
does not know how to do and reason: I don't know X → research → practice → verify → remember → reuse.
It does NOT hardcode any specific capability (§12) — capabilities are data, discovered from experience.
"""

from __future__ import annotations

import contextlib
from typing import Any
from uuid import UUID, uuid4

# Confidence floor per status — evidence, not the model's say-so (§14). Success/failure ratio adjusts it.
_STATUS_CONF: dict[str, float] = {
    "unknown": 0.10, "researched": 0.30, "attempted": 0.40, "successful": 0.60,
    "verified": 0.85, "available": 0.90, "degraded": 0.40, "deprecated": 0.10,
}
_STATUS_RANK = {s: i for i, s in enumerate(
    ("unknown", "researched", "attempted", "successful", "verified", "available"))}

# Consecutive failed USES that mark a capability degraded. A single failure is a blip (a bad argument, a
# busy host); three in a row is the capability itself being broken. History is preserved either way.
_DEGRADE_AFTER = 3


def _confidence(status: str, succ: int, fail: int) -> float:
    base = _STATUS_CONF.get(status, 0.1)
    attempts = succ + fail
    if attempts:
        base += 0.1 * (succ / attempts) - 0.1 * (fail / attempts)
    return round(max(0.05, min(0.99, base)), 3)


# ── FUZZY NAME MATCH: the anti-relearn cornerstone ─────────────────────────────────────────
#
# Almir's requirement (§7): "Sali should not repeatedly rediscover the same capability." The naming
# model — one inference per completed task — produces natural short names, and two semantically
# identical objectives can yield different words: "create python archive script" one day, "write
# python archive script" the next. `observe()` uniques on the exact string, so both landed as
# separate rows. Live proof from the DB before this fix: three separate rows for the same skill,
# each with its own use_count that never accumulated on one row.
#
# The normaliser strips the leading verb (create / write / make / build / generate / produce / set /
# configure / set-up) that carries no distinguishing meaning, drops stopwords ("a / the / one"),
# lowercases, and joins the remaining tokens. Two capabilities whose normalised forms match are the
# same skill and reuse the same row.

_VERB_SYNONYMS = frozenset({
    "create", "make", "build", "generate", "produce", "write", "set", "setup", "configure",
    "install", "add", "compose", "assemble", "prepare",
})
_STOPWORDS = frozenset({"a", "an", "the", "one", "some", "any", "to", "for", "of", "with", "in", "on"})


def _normalise_name(name: str) -> str:
    """Two rows with the same normalised form are the SAME skill. Kept deliberately conservative -
    a false merge (two distinct skills collapsed) is worse than a false split (the old behaviour),
    because a merge cannot be undone from data and would attribute one skill's failures to another."""
    tokens = [t for t in (name or "").lower().split() if t and t not in _STOPWORDS]
    # Strip the leading verb ("create X", "write X") and its trailing preposition when the phrase
    # is verb+prep ("set up X" - "up" carries no distinguishing meaning here). Doing the two-token
    # peel is what makes "configure an nginx vhost" and "set up an nginx vhost" match.
    while tokens and (tokens[0] in _VERB_SYNONYMS or tokens[0] in {"up", "out", "together"} and len(tokens) > 1):
        tokens = tokens[1:]
    return " ".join(tokens)


class CapabilityStore:
    def __init__(self, pool: Any, publisher: Any = None) -> None:
        self._pool = pool
        self._publisher = publisher

    async def observe(
        self, *, name: str, scope: str = "environment", scope_ref: str | None = None,
        status: str = "unknown", supported_by: dict[str, Any] | None = None,
    ) -> UUID:
        """Record (or update) a capability at a given status. Deduped by (name, scope, scope_ref): the
        status only ever moves FORWARD along the ladder (unknown→…→available) unless explicitly degraded,
        so re-observing never regresses hard-won evidence."""
        async with self._pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT id, status, times_succeeded, times_failed FROM capability "
                "WHERE name=$1 AND scope=$2 AND coalesce(scope_ref,'')=coalesce($3,'')",
                name, scope, scope_ref)
            if existing is not None:
                new_status = self._max_status(existing["status"], status)
                conf = _confidence(new_status, existing["times_succeeded"], existing["times_failed"])
                await conn.execute(
                    "UPDATE capability SET status=$2, confidence=$3, supported_by = supported_by || $4::jsonb, "
                    "  updated_at=now() WHERE id=$1", existing["id"], new_status, conf, supported_by or {})
                return UUID(str(existing["id"]))
            # ANTI-RELEARN. Before inserting a new row, check whether an existing capability in the
            # same scope IS THE SAME SKILL under a differently-worded name. The naming model varies its
            # verb ("create/write/build") and picks slightly different phrasings for objectives that
            # describe the same work - and observe() uniqued on the exact string, so semantically
            # identical skills landed as separate rows and never accumulated evidence on one.
            #
            # If a match is found: reuse THAT row. Its use_count, its acquisition history, its
            # verified status all continue accruing on the one capability instead of scattering across
            # near-duplicates.
            target = _normalise_name(name)
            if target:
                sibling = await conn.fetchrow(
                    "SELECT id, name, status, times_succeeded, times_failed FROM capability "
                    "WHERE scope=$1 AND coalesce(scope_ref,'')=coalesce($2,'') AND name <> $3",
                    scope, scope_ref, name)
                # Cheap: fetch and normalise per-row, one-by-one, only inside a scope. A scope
                # typically holds fewer than a few hundred capabilities so this is bounded and fast.
                # A future indexed lookup on a stored normalised column can replace this if it ever
                # becomes hot.
                candidates = await conn.fetch(
                    "SELECT id, name, status, times_succeeded, times_failed FROM capability "
                    "WHERE scope=$1 AND coalesce(scope_ref,'')=coalesce($2,'')",
                    scope, scope_ref)
                match = next((r for r in candidates if _normalise_name(r["name"]) == target), None)
                if match is not None:
                    new_status = self._max_status(match["status"], status)
                    conf = _confidence(new_status, match["times_succeeded"], match["times_failed"])
                    await conn.execute(
                        "UPDATE capability SET status=$2, confidence=$3, "
                        "  supported_by = supported_by || $4::jsonb, updated_at=now() WHERE id=$1",
                        match["id"], new_status, conf, supported_by or {})
                    await self._emit("capability.merged",
                                     {"capability_id": str(match["id"]),
                                      "kept_name": match["name"], "coined": name})
                    return UUID(str(match["id"]))
            cid = uuid4()
            await conn.execute(
                "INSERT INTO capability (id, name, status, scope, scope_ref, confidence, supported_by) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7)",
                cid, name, status, scope, scope_ref, _confidence(status, 0, 0), supported_by or {})
        await self._emit("capability.discovered", {"capability_id": str(cid), "name": name,
                                                   "scope": scope, "status": status})
        return cid

    async def record_attempt(
        self, *, name: str, success: bool, scope: str = "environment", scope_ref: str | None = None,
        verified: bool = False, supported_by: dict[str, Any] | None = None,
    ) -> UUID:
        """An actual attempt to use the capability. Success advances it (→successful, or →verified when
        reviewer/independently verified); failure is recorded as negative evidence and never advances it
        past 'attempted' (§14). Confidence is derived from the running success/failure ratio."""
        cid = await self.observe(name=name, scope=scope, scope_ref=scope_ref,
                                 status="attempted", supported_by=supported_by)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT status, times_succeeded, times_failed FROM capability WHERE id=$1", cid)
            if success:
                new_status = self._max_status(row["status"], "verified" if verified else "successful")
                succ, fail = row["times_succeeded"] + 1, row["times_failed"]
            else:
                new_status = row["status"]                      # a failure never advances the status (§14)
                succ, fail = row["times_succeeded"], row["times_failed"] + 1
            conf = _confidence(new_status, succ, fail)
            await conn.execute(
                "UPDATE capability SET status=$2, times_succeeded=$3, times_failed=$4, confidence=$5, "
                "  last_verified = CASE WHEN $6 THEN now() ELSE last_verified END, updated_at=now() "
                "WHERE id=$1", cid, new_status, succ, fail, conf, success and verified)
        await self._emit("capability.verified" if (success and verified) else
                         ("capability.attempted" if success else "capability.failed"),
                         {"capability_id": str(cid), "name": name, "success": success})
        return cid

    async def from_experience(self, record: dict[str, Any]) -> UUID | None:
        """Derive a capability candidate from a VERIFIED experience (§49). Scoped to the experience's
        environment — a single verified task establishes 'I can do this HERE', never a universal rule
        (generalization needs more evidence, §31/§49). Points at the experience + procedure that back it."""
        if record.get("evidence_state") != "verified" or not record.get("procedure"):
            return None
        # The reusable SKILL name when the experience layer could derive one, the objective only as a
        # last resort. Naming a capability after its objective is what made every capability unmatchable.
        name = (record.get("capability_name") or record.get("objective") or "").strip()[:120]
        if not name:
            return None
        scope_ref = record.get("scope_ref") or "local"
        supported = {"experiences": [record.get("memory_id")] if record.get("memory_id") else [],
                     "procedures": [record.get("procedure")], "research": record.get("research", [])}
        cap_id = await self.record_attempt(
            name=name, success=True, verified=True, scope="environment", scope_ref=scope_ref,
            supported_by=supported)
        # Keep the human sentence too: the NAME has to be stable enough to match again, so the concrete
        # thing that was actually accomplished lives in `purpose` where a person can read it. This is
        # what `set_purpose` was written for and it had no caller.
        objective = (record.get("objective") or "").strip()
        if cap_id is not None and objective and objective[:120] != name:
            with contextlib.suppress(Exception):
                await self.set_purpose(name=name, purpose=objective[:400],
                                       scope="environment", scope_ref=scope_ref)
        return cap_id

    async def get(self, *, name: str, scope: str = "environment", scope_ref: str | None = None) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, name, status, scope, scope_ref, confidence, supported_by, depends_on, "
                "  times_succeeded, times_failed, last_verified, purpose, resource_id, last_used, "
                "  use_count FROM capability "
                "WHERE name=$1 AND scope=$2 AND coalesce(scope_ref,'')=coalesce($3,'')",
                name, scope, scope_ref)
        return dict(row) if row else None

    # ── purpose, resource link & utilization (Prompt 8B §4/§6/§12/§19) ───────────────────────────────
    async def set_purpose(self, *, name: str, purpose: str, scope: str = "environment",
                          scope_ref: str | None = None, resource_id: UUID | None = None) -> None:
        """Record WHY a capability was acquired and, optionally, the backing resource (an account/service
        modelled as an external_entity, §6). Purpose survives the acquisition task (§19)."""
        await self.observe(name=name, scope=scope, scope_ref=scope_ref)
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE capability SET purpose=$4, resource_id=coalesce($5, resource_id), updated_at=now() "
                "WHERE name=$1 AND scope=$2 AND coalesce(scope_ref,'')=coalesce($3,'')",
                name, scope, scope_ref, purpose, resource_id)

    async def record_use(
        self, *, name: str, scope: str = "environment", scope_ref: str | None = None,
        purpose: str | None = None, action: str | None = None, result: str | None = None,
        success: bool = True, verified: bool = False, task_id: UUID | None = None,
        run_id: UUID | None = None,
    ) -> bool:
        """Record that Sali actually USED a capability (§12) — the evidence that it's part of Sali's
        operational life, not a memory of reading about it. Logs the usage, stamps last_used, and (on a
        verified success) can promote status to 'available'. Returns False if the capability doesn't exist."""
        got = await self.get(name=name, scope=scope, scope_ref=scope_ref)
        if got is None:
            return False
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO capability_usage (capability_id, task_id, run_id, purpose, action, result, "
                "  success, verified) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
                got["id"], task_id, run_id, purpose, action, (result or "")[:500], success, verified)
            await conn.execute(
                "UPDATE capability SET last_used=now(), use_count=use_count+1, "
                "  status = CASE WHEN $2 AND $3 AND status='verified' THEN 'available' ELSE status END, "
                "  last_verified = CASE WHEN $3 THEN now() ELSE last_verified END, "
                "  times_failed = times_failed + CASE WHEN $2 THEN 0 ELSE 1 END, updated_at=now() "
                "WHERE id=$1", got["id"], success, verified)
            # A failed use is negative evidence and must MOVE the number, or a capability that has since
            # broken stays advertised at confidence 0.9 forever. times_succeeded is deliberately NOT
            # incremented here: record_attempt already counts the success at acquisition, and counting
            # the same task twice would inflate the ratio the confidence is derived from.
            row = await conn.fetchrow(
                "SELECT status, times_succeeded, times_failed FROM capability WHERE id=$1", got["id"])
            await conn.execute(
                "UPDATE capability SET confidence=$2 WHERE id=$1", got["id"],
                _confidence(row["status"], row["times_succeeded"], row["times_failed"]))
            recent = await conn.fetch(
                "SELECT success FROM capability_usage WHERE capability_id=$1 "
                "ORDER BY created_at DESC LIMIT $2", got["id"], _DEGRADE_AFTER)
        await self._emit("capability.used", {"name": name, "success": success, "verified": verified})
        # The loop that makes 'available' mean something today rather than on the day it was learned:
        # _DEGRADE_AFTER consecutive failed uses degrade it, and a later success restores it. Without
        # this the failure evidence above is inert — written, then never read by anything.
        if (not success and len(recent) >= _DEGRADE_AFTER
                and not any(r["success"] for r in recent)
                and got["status"] not in ("degraded", "deprecated")):
            await self.degrade(
                name=name, scope=scope, scope_ref=scope_ref,
                reason=f"failed {_DEGRADE_AFTER} uses in a row; last: {(result or '')[:80]}")
        elif success and got["status"] in ("degraded", "deprecated"):
            await self.restore(name=name, scope=scope, scope_ref=scope_ref, verified=verified)
        return True

    async def usage_history(self, *, name: str, scope: str = "environment",
                            scope_ref: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        got = await self.get(name=name, scope=scope, scope_ref=scope_ref)
        if got is None:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT purpose, action, result, success, verified, created_at FROM capability_usage "
                "WHERE capability_id=$1 ORDER BY created_at DESC LIMIT $2", got["id"], limit)
        return [dict(r) for r in rows]

    async def check_availability(
        self, *, name: str, scope: str = "environment", scope_ref: str | None = None,
        max_age_seconds: int = 604800,
    ) -> str:
        """Whether a capability is usable RIGHT NOW, distinguishing known / recently-verified / currently-
        available (§13). Returns 'available' (fresh + usable), 'stale' (usable but not recently verified
        → re-verify before critical use), 'degraded', or 'unavailable'/'unknown'. Deterministic, from
        durable state — never assumes an old capability still works."""
        got = await self.get(name=name, scope=scope, scope_ref=scope_ref)
        if got is None:
            return "unknown"
        if got["status"] in ("degraded", "deprecated"):
            return "degraded"
        if got["status"] not in ("verified", "available"):
            return "unavailable"
        async with self._pool.acquire() as conn:
            fresh = await conn.fetchval(
                "SELECT last_verified >= now() - make_interval(secs => $2) FROM capability WHERE id=$1",
                got["id"], max_age_seconds)
        return "available" if fresh else "stale"

    async def already_have(self, *, name: str, scope_ref: str | None = None) -> bool:
        """Avoid duplicate acquisition (§16): whether a usable capability by this name already exists in
        this environment or globally — check before learning/installing/creating it again."""
        return await self.is_usable(name=name, scope_ref=scope_ref)

    async def discover_for_task(self, objective: str, *, scope_ref: str | None = None,
                                limit: int = 5) -> list[dict[str, Any]]:
        """During planning, find capabilities Sali ALREADY possesses that are relevant to a task, so it
        uses existing competence instead of relearning from zero (§8/§9/§22). Verified/available first."""
        return await self.for_goal(objective, scope_ref=scope_ref, limit=limit)

    # ── dependencies / composition (§8/§9) ──────────────────────────────────────────────────────────
    async def set_dependencies(
        self, *, name: str, depends_on: list[str], scope: str = "environment",
        scope_ref: str | None = None,
    ) -> None:
        """Declare that a capability requires other capabilities (composition, §8). The gap analysis
        resolves the dependency list against what is actually verified/available."""
        await self.observe(name=name, scope=scope, scope_ref=scope_ref)
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE capability SET depends_on=$4, updated_at=now() "
                "WHERE name=$1 AND scope=$2 AND coalesce(scope_ref,'')=coalesce($3,'')",
                name, scope, scope_ref, depends_on)

    async def gap_analysis(
        self, required: list[str], *, scope_ref: str | None = None,
    ) -> dict[str, Any]:
        """Given the capabilities an objective needs, split them into what Sali can actually do
        (verified/available, in this environment or globally) and what is MISSING (§7/§10). A missing
        capability is never hallucinated as present. Deterministic — the DB is the authority."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT name FROM capability WHERE name = ANY($1::text[]) "
                "  AND status IN ('verified','available') "
                "  AND (scope='global' OR coalesce(scope_ref,'')=coalesce($2,''))",
                required, scope_ref)
        have = {r["name"] for r in rows}
        missing = [c for c in required if c not in have]
        return {"required": required, "available": sorted(have), "missing": missing}

    async def for_goal(self, objective: str, *, scope_ref: str | None = None,
                       limit: int = 5) -> list[dict[str, Any]]:
        """Capabilities relevant to a new goal, usable ones first — 'I have done this before' (§38/§70).
        Deterministic keyword overlap over capability names; bounded."""
        import re
        keys = {w for w in re.findall(r"[a-z0-9]+", (objective or "").lower()) if len(w) >= 3}
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT name, status, scope, scope_ref, confidence, times_succeeded FROM capability "
                "ORDER BY confidence DESC LIMIT 200")
        scored: list[tuple[int, dict[str, Any]]] = []
        for r in rows:
            overlap = len({w for w in re.findall(r"[a-z0-9]+", r["name"].lower()) if len(w) >= 3} & keys)
            if overlap == 0:
                continue
            bonus = 2 if r["status"] in ("verified", "available") else 0
            if scope_ref and r["scope_ref"] == scope_ref:
                bonus += 1
            scored.append((overlap + bonus, dict(r)))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [rec for _, rec in scored[:limit]]

    # ── regression: a capability can degrade without erasing its history (§30/§71) ───────────────────
    async def degrade(
        self, *, name: str, scope: str = "environment", scope_ref: str | None = None,
        reason: str, deprecated: bool = False,
    ) -> bool:
        """Mark a capability currently unusable (dependency gone, credential expired, API changed, …)
        WITHOUT deleting its history — the successful past stays recorded; only current availability
        changes (§30). Returns True if a capability was degraded."""
        status = "deprecated" if deprecated else "degraded"
        async with self._pool.acquire() as conn:
            row = await conn.fetchval(
                "UPDATE capability SET status=$4, confidence=least(confidence, 0.4), "
                "  evidence = evidence || jsonb_build_object('degraded_reason', $5::text), updated_at=now() "
                "WHERE name=$1 AND scope=$2 AND coalesce(scope_ref,'')=coalesce($3,'') RETURNING id",
                name, scope, scope_ref, status, reason[:200])
        if row is None:
            return False
        await self._emit("capability.degraded", {"name": name, "scope": scope, "reason": reason[:120]})
        return True

    async def restore(
        self, *, name: str, scope: str = "environment", scope_ref: str | None = None,
        verified: bool = True,
    ) -> bool:
        """A previously-degraded capability that works again → back to verified/available, history
        preserved and appended to (§71). Returns True if restored."""
        got = await self.get(name=name, scope=scope, scope_ref=scope_ref)
        if got is None or got["status"] not in ("degraded", "deprecated"):
            return False
        await self.record_attempt(name=name, success=True, verified=verified, scope=scope,
                                  scope_ref=scope_ref)
        await self._emit("capability.verified", {"name": name, "scope": scope, "restored": True})
        return True

    async def is_usable(self, *, name: str, scope_ref: str | None = None) -> bool:
        """Whether Sali can actually do this now — verified/available here or globally (§33 distinguishes
        'knows how' from 'currently able'). A degraded/historical capability returns False."""
        async with self._pool.acquire() as conn:
            return bool(await conn.fetchval(
                "SELECT 1 FROM capability WHERE name=$1 AND status IN ('verified','available') "
                "  AND (scope='global' OR coalesce(scope_ref,'')=coalesce($2,'')) LIMIT 1",
                name, scope_ref))

    async def list(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        clause = ""
        args: list[Any] = []
        if status is not None:
            args.append(status)
            clause = " WHERE status=$1"
        args.append(limit)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, name, status, scope, scope_ref, confidence, times_succeeded, times_failed, "
                "  purpose, last_used, use_count, last_verified "
                f"FROM capability{clause} ORDER BY confidence DESC, updated_at DESC LIMIT ${len(args)}", *args)
        return [dict(r) for r in rows]

    async def counts(self) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT status, count(*) AS n FROM capability GROUP BY status")
        by = {r["status"]: int(r["n"]) for r in rows}
        usable = by.get("verified", 0) + by.get("available", 0)
        return {"total": sum(by.values()), "usable": usable, "by_status": by}

    @staticmethod
    def _max_status(current: str, candidate: str) -> str:
        """Move forward along the ladder; never regress a verified capability to a weaker status here
        (degradation/deprecation is an explicit, separate action)."""
        if current in ("degraded", "deprecated"):
            return candidate
        return current if _STATUS_RANK.get(current, 0) >= _STATUS_RANK.get(candidate, 0) else candidate

    async def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        with contextlib.suppress(Exception):
            await self._publisher.emit(event_type=event_type, subject_type="capability",
                                       origin="learning", data=data)
