"""Per-binary tool-experience mining (spec §16/§46/§55/§67).

The durable ``tool_execution`` record holds every command Sali ran. Its ``tool_name`` is the CODE
tool ('execute_command'); the actual binary lives inside ``plan->args->command``. This miner
aggregates, PER UNDERLYING BINARY, how it has actually behaved on THIS machine — how often it
succeeded, how long it typically takes, and its common failure modes — and folds that into the
existing memory as a ``tool:<binary>`` fact with ``certainty:'learned'``.

Evidence-gated (§46): a binary is never characterised from a single run — it needs at least
``threshold`` executions. The evolving statistics are refreshed in place on each pass (like the
twin refreshing machine state) rather than re-asserted as a new claim value, so an ever-changing
success rate never logs false contradictions under the stable ``tool:<binary>`` key.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sali.core.enums import MemoryLayer, MemorySource
from sali.core.toolvocab import binary_of
from sali.memory import writer as memory_writer
from sali.obs.log import get_logger

log = get_logger("sali.learning.tool_experience")

_MAX_FAILURE_MODES = 3


@dataclass(slots=True)
class ToolExperience:
    binary: str
    runs: int
    successes: int
    failures: int
    reliability: float
    latency_p50_ms: int | None


def _p50(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def _failure_modes(errors: list[str]) -> list[str]:
    counts: dict[str, int] = {}
    for err in errors:
        sig = (err or "").strip().splitlines()[0][:120] if err and err.strip() else ""
        if sig:
            counts[sig] = counts.get(sig, 0) + 1
    return [s for s, _ in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:_MAX_FAILURE_MODES]]


async def learn_tool_experiences(
    conn: Any, *, threshold: int = 3, limit: int = 5000
) -> list[ToolExperience]:
    """Mine per-tool reliability/latency/failure-modes from tool_execution into ``tool:<name>``
    memories that cleared the evidence bar. Covers ALL tools, not just execute_command.
    For execute_command, tracks per-binary; for other tools, tracks per tool_name.
    Caller owns the transaction."""
    rows = await conn.fetch(
        "SELECT tool_name, plan->'args'->>'command' AS command, success, duration_ms, error "
        "FROM tool_execution ORDER BY started_at DESC LIMIT $1",
        limit,
    )

    agg: dict[str, dict[str, Any]] = {}
    for r in rows:
        # For execute_command, track per-binary; for others, track per tool_name
        if r["tool_name"] == "execute_command":
            cmd = r["command"]
            if not cmd or not str(cmd).strip():
                continue  # skip commandless executions (planned, aborted, etc.)
            key = binary_of(str(cmd))
            if not key:
                continue
        else:
            key = r["tool_name"]
        a = agg.setdefault(key, {"runs": 0, "ok": 0, "fail": 0, "durations": [], "errors": []})
        a["runs"] += 1
        if r["success"] is True:
            a["ok"] += 1
        elif r["success"] is False:
            a["fail"] += 1
            if r["error"]:
                a["errors"].append(str(r["error"]))
        if r["duration_ms"] is not None:
            a["durations"].append(int(r["duration_ms"]))

    learned: list[ToolExperience] = []
    for binary, a in agg.items():
        if a["runs"] < threshold:
            continue  # never characterise a tool from too little evidence (§46)
        reliability = round(a["ok"] / a["runs"], 3)
        p50 = _p50(a["durations"])
        modes = _failure_modes(a["errors"])
        structured = {
            "runs": a["runs"], "successes": a["ok"], "failures": a["fail"],
            "reliability": reliability, "latency_p50_ms": p50, "failure_modes": modes,
            "certainty": "learned",
        }
        content = (f"{binary}: run {a['runs']}× on this machine, {reliability:.0%} success"
                   + (f", ~{p50}ms typical" if p50 is not None else ""))
        conf = min(0.9, 0.5 + 0.02 * a["runs"])
        await _remember_experience(conn, binary, content, structured, conf, a["runs"])
        learned.append(ToolExperience(binary=binary, runs=a["runs"], successes=a["ok"],
                                      failures=a["fail"], reliability=reliability, latency_p50_ms=p50))
    return learned


async def _remember_experience(
    conn: Any, binary: str, content: str, structured: dict[str, Any], conf: float, runs: int
) -> None:
    claim_key = f"tool:{binary}"
    existing = await conn.fetchrow(
        "SELECT id FROM memory WHERE claim_key=$1 AND valid_until IS NULL", claim_key)
    if existing is not None:
        # Refresh the evolving aggregate in place — new-wins on the metrics, no contradiction churn.
        await conn.execute(
            "UPDATE memory SET content=$2, structured=$3, confidence=$4, "
            "  evidence_count=greatest(evidence_count, $5), last_seen=now(), last_verified=now(), "
            "  updated_at=now() WHERE id=$1",
            existing["id"], content, structured, conf, runs)
        return
    await memory_writer.remember(
        conn, layer=MemoryLayer.SEMANTIC, content=content, source=MemorySource.INFERENCE,
        functional=True, claim_key=claim_key, importance=0.5, obs_conf=conf, structured=structured)
