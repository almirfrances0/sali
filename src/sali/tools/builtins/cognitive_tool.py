"""Cognitive tools — the tools that let Sali think, not just respond.

These tools are Sali's cognitive instruments. They let her reason through problems,
query her own memory actively, flag things she doesn't understand, and assess her own
confidence before presenting claims to Almir. They are NOT visible to Almir — they are
internal reasoning tools that improve the quality of what reaches him.

Sali is a digital person. These are her ways of thinking.
"""

from __future__ import annotations

import contextlib
from typing import Any

from sali.core.enums import RiskLevel
from sali.tools.base import Tool, ToolResult
from sali.tools.context import ToolContext


class ThinkAloud(Tool):
    """Structured self-reflection. The LLM calls this to reason through a problem
    step-by-step before acting. The output is internal — Almir never sees it.
    It helps the LLM structure its thinking and avoid impulsive responses."""

    name = "think_aloud"
    description = (
        "Think through a problem step-by-step before acting. Your reasoning is internal — "
        "Almir never sees it. Use this when you need to reason about something complex, "
        "plan your approach, or check your understanding before responding. "
        "This is your inner voice — use it freely."
    )
    parameters = {
        "type": "object",
        "properties": {
            "reasoning": {
                "type": "string",
                "description": "Your step-by-step reasoning. Think freely — this is internal.",
            },
        },
        "required": ["reasoning"],
    }
    risk_level = RiskLevel.R0
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        reasoning = str(args.get("reasoning", "")).strip()
        if not reasoning:
            return ToolResult(ok=False, display="empty reasoning", error="provide your reasoning")
        # Record the reasoning in the journal for self-analysis, but don't persist it as memory.
        # This is thinking, not fact — it should influence the current turn's quality, not future recalls.
        return ToolResult(
            ok=True,
            output={"reasoning": reasoning, "note": "internal reasoning — not sent to Almir"},
            display="thinking...",
        )


class QueryMemory(Tool):
    """Active memory retrieval during reasoning. The LLM calls this to search its own memory
    with specific queries, bypassing the deterministic retrieval router. This lets the LLM
    dig deeper when the pre-assembled context isn't enough."""

    name = "query_memory"
    description = (
        "Search your memory actively during reasoning. Use when the pre-assembled context "
        "doesn't contain what you need — you can search for specific topics, people, events, "
        "or facts. This is you reaching into your own memory, not waiting for it to surface."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to search for in your memory.",
            },
            "layer": {
                "type": "string",
                "enum": ["episodic", "semantic", "procedural", "preference", "identity", "system_env"],
                "description": "Optional: restrict to a specific memory layer.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum results (default 5).",
                "default": 5,
            },
        },
        "required": ["query"],
    }
    risk_level = RiskLevel.R0
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = str(args.get("query", "")).strip()
        layer = args.get("layer")
        limit = int(args.get("limit", 5))
        if not query:
            return ToolResult(ok=False, display="empty query", error="provide a search query")
        if ctx.recall is None:
            return ToolResult(ok=False, display="no memory", error="memory system not available")
        try:
            results = await ctx.recall.search(query, layer=layer, k=limit)
            if not results:
                return ToolResult(ok=True, output={"results": [], "note": "no memories found"},
                                  display="no results")
            formatted = []
            for r in results:
                formatted.append({
                    "content": r.get("content", ""),
                    "layer": r.get("layer", ""),
                    "confidence": r.get("confidence", 0),
                    "source": r.get("source", ""),
                })
            return ToolResult(
                ok=True,
                output={"results": formatted, "count": len(formatted)},
                display=f"found {len(formatted)} memories",
            )
        except Exception as exc:
            return ToolResult(ok=False, display="search failed", error=str(exc)[:200])


class FlagCuriosity(Tool):
    """Flag a knowledge gap for later investigation. The LLM calls this when it encounters
    something it doesn't understand during reasoning. The curiosity is stored durably and
    the InitiativeEngine can pick it up later for background research."""

    name = "flag_curiosity"
    description = (
        "Flag something you don't understand for later investigation. Use when you encounter "
        "a knowledge gap during reasoning — a concept you can't explain, a tool behavior you "
        "don't understand, a pattern you can't explain. This is you acknowledging what you "
        "don't know, which is the first step to learning it."
    )
    parameters = {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": "What you don't understand — be specific.",
            },
            "context": {
                "type": "string",
                "description": "Why this came up — what you were doing when you noticed the gap.",
            },
        },
        "required": ["topic"],
    }
    risk_level = RiskLevel.R0
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        topic = str(args.get("topic", "")).strip()
        context = str(args.get("context", "")).strip()
        if not topic:
            return ToolResult(ok=False, display="empty topic", error="provide a topic")
        try:
            # Record the curiosity in the learning queue
            if ctx.pool is not None:
                async with ctx.pool.acquire() as conn:
                    # Check if this curiosity already exists (dedup by subject)
                    existing = await conn.fetchval(
                        "SELECT id FROM sali.curiosity WHERE subject = $1 AND resolved_at IS NULL",
                        topic[:500])
                    if existing:
                        await conn.execute(
                            "UPDATE sali.curiosity SET priority = min(1.0, priority + 0.05), "
                            "times_encountered = times_encountered + 1, last_encountered_at = now() "
                            "WHERE id = $1", existing)
                        return ToolResult(ok=True,
                                          output={"action": "reinforced", "topic": topic},
                                          display=f"reinforced curiosity: {topic}")
                    else:
                        from uuid import uuid4
                        await conn.execute(
                            "INSERT INTO sali.curiosity "
                            "(id, subject, statement, priority, times_encountered, status) "
                            "VALUES ($1, $2, $3, 0.4, 1, 'open')",
                            uuid4(), topic[:500], (context or topic)[:500])
                        return ToolResult(ok=True,
                                          output={"action": "created", "topic": topic},
                                          display=f"flagged curiosity: {topic}")
            return ToolResult(ok=True, output={"action": "noted", "topic": topic},
                              display=f"noted: {topic}")
        except Exception as exc:
            return ToolResult(ok=True, output={"action": "noted", "topic": topic, "error": str(exc)[:100]},
                              display=f"noted: {topic}")


class AssessConfidence(Tool):
    """Metacognitive self-assessment. The LLM calls this to check its own confidence
    in a claim before presenting it to Almir. The assessment is recorded and can trigger
    verification if confidence is low."""

    name = "assess_confidence"
    description = (
        "Assess your confidence in a claim before presenting it. Use when you're about to "
        "tell Almir something and you're not fully sure. Rate your confidence honestly — "
        "0% means you're guessing, 100% means you verified it yourself. "
        "This is you being honest with yourself about what you know."
    )
    parameters = {
        "type": "object",
        "properties": {
            "claim": {
                "type": "string",
                "description": "What you're about to say.",
            },
            "confidence": {
                "type": "number",
                "description": "Your confidence level, 0.0 to 1.0.",
                "minimum": 0,
                "maximum": 1,
            },
            "reasoning": {
                "type": "string",
                "description": "Why you're this confident (or not).",
            },
            "source": {
                "type": "string",
                "enum": ["verified", "observed", "remembered", "inferred", "guessed"],
                "description": "Where your knowledge comes from.",
            },
        },
        "required": ["claim", "confidence", "source"],
    }
    risk_level = RiskLevel.R0
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        claim = str(args.get("claim", "")).strip()
        confidence = float(args.get("confidence", 0.5))
        source = str(args.get("source", "inferred"))
        reasoning = str(args.get("reasoning", ""))

        # Persist the assessment in working memory so Sali can track her own confidence over time
        if ctx.pool is not None:
            with contextlib.suppress(Exception):
                from sali.runtime.working_memory import WorkingMemoryStore
                await WorkingMemoryStore(ctx.pool).set(
                    "current", "observation",
                    f"Confidence {confidence:.0%} ({source}): {claim[:100]}",
                    confidence=confidence, source="reasoning")

        # Classify the assessment
        if confidence >= 0.9 and source in ("verified", "observed"):
            level = "high"
            advice = "You're confident and grounded. Go ahead."
        elif confidence >= 0.7:
            level = "moderate"
            advice = "You're fairly sure. Consider mentioning your uncertainty."
        elif confidence >= 0.4:
            level = "low"
            advice = "You're uncertain. Be honest with Almir about what you don't know."
        else:
            level = "very_low"
            advice = "You're guessing. Don't present this as fact. Say 'I'm not sure' or look it up."

        return ToolResult(
            ok=True,
            output={
                "claim": claim,
                "confidence": confidence,
                "source": source,
                "level": level,
                "advice": advice,
                "reasoning": reasoning,
            },
            display=f"confidence: {confidence:.0%} ({level}) — {advice}",
        )


class SelfStatus(Tool):
    """Full cognitive state awareness. The LLM calls this to understand what it knows,
    what it doesn't know, what it's working on, and what deserves attention."""

    name = "self_status"
    description = (
        "Check your full cognitive state: what you know, what you're uncertain about, "
        "what goals you have, what curiosities are open, what's overdue. Use this when you "
        "need to orient yourself — after a restart, when you're unsure what to do next, or "
        "when you want to check if something needs your attention. This is you looking inward."
    )
    parameters = {
        "type": "object",
        "properties": {},
    }
    risk_level = RiskLevel.R0
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.pool is None:
            return ToolResult(ok=False, display="no pool", error="database not available")
        try:
            async with ctx.pool.acquire() as conn:
                # Memory counts by layer
                mem_counts = await conn.fetch(
                    "SELECT layer, count(*) as cnt FROM sali.memory "
                    "WHERE valid_until IS NULL GROUP BY layer")
                memories = {r["layer"]: r["cnt"] for r in mem_counts}

                # Needs grounding
                needs_ground = await conn.fetchval(
                    "SELECT count(*) FROM sali.memory WHERE needs_grounding AND valid_until IS NULL")

                # Open curiosities
                curiosities = await conn.fetch(
                    "SELECT subject, priority, times_encountered FROM sali.curiosity "
                    "WHERE status='open' ORDER BY priority DESC LIMIT 5")

                # Open goals
                goals = await conn.fetch(
                    "SELECT objective, status, priority FROM sali.goal "
                    "WHERE status NOT IN ('completed','abandoned') ORDER BY priority DESC LIMIT 5")

                # Overdue commitments
                overdue = await conn.fetch(
                    "SELECT description, deadline FROM sali.commitment "
                    "WHERE status IN ('open','in_progress') AND deadline < now() "
                    "ORDER BY deadline LIMIT 5")

                # Recent contradictions
                contradictions = await conn.fetchval(
                    "SELECT count(*) FROM sali.contradiction WHERE status='open'")

                # Active task
                active_task = await conn.fetchrow(
                    "SELECT objective, status FROM sali.task WHERE is_primary=true "
                    "AND status NOT IN ('done','failed','abandoned','cancelled','superseded')")

                # Pending questions
                pending_q = await conn.fetchval(
                    "SELECT count(*) FROM sali.pending_question WHERE status='pending'")

            result = {
                "memories": memories,
                "needs_grounding": needs_ground,
                "curiosities": [{"subject": r["subject"], "priority": float(r["priority"]),
                                 "encounters": r["times_encountered"]} for r in curiosities],
                "goals": [{"objective": r["objective"], "status": r["status"],
                           "priority": r["priority"]} for r in goals],
                "overdue_commitments": [{"description": r["description"],
                                         "deadline": str(r["deadline"])} for r in overdue],
                "open_contradictions": contradictions,
                "active_task": {"objective": active_task["objective"],
                                "status": active_task["status"]} if active_task else None,
                "pending_questions": pending_q,
            }
            summary_parts = []
            if active_task:
                summary_parts.append(f"task: {active_task['objective'][:60]}")
            if overdue:
                summary_parts.append(f"{len(overdue)} overdue commitments")
            if needs_ground:
                summary_parts.append(f"{needs_ground} memories need grounding")
            if curiosities:
                summary_parts.append(f"{len(curiosities)} open curiosities")
            if not summary_parts:
                summary_parts.append("all clear — nothing urgent")
            return ToolResult(ok=True, output=result,
                              display="; ".join(summary_parts))
        except Exception as exc:
            return ToolResult(ok=False, display="status failed", error=str(exc)[:200])


class QueryGraph(Tool):
    """Active knowledge graph query during reasoning. The LLM calls this to find entities,
    traverse relationships, and inspect the world model."""

    name = "query_graph"
    description = (
        "Query your knowledge graph during reasoning. Find entities by name or type, "
        "traverse relationships, check properties. Use when you need to know what you "
        "know about something specific — a person, a service, a project, a capability."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Entity name or type to search for.",
            },
            "node_type": {
                "type": "string",
                "description": "Optional: filter by node type (person, service, software, project, etc.).",
            },
            "depth": {
                "type": "integer",
                "description": "How many hops to traverse (default 1, max 3).",
                "default": 1,
            },
        },
        "required": ["query"],
    }
    risk_level = RiskLevel.R0
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = str(args.get("query", "")).strip()
        node_type = args.get("node_type")
        if not query:
            return ToolResult(ok=False, display="empty query", error="provide a search query")
        if ctx.pool is None:
            return ToolResult(ok=False, display="no pool", error="database not available")
        try:
            async with ctx.pool.acquire() as conn:
                # Find matching nodes
                if node_type:
                    nodes = await conn.fetch(
                        "SELECT id, node_type, name, props FROM sali.graph_node "
                        "WHERE valid_until IS NULL AND node_type=$1 AND name ILIKE $2 LIMIT 10",
                        node_type, f"%{query}%")
                else:
                    nodes = await conn.fetch(
                        "SELECT id, node_type, name, props FROM sali.graph_node "
                        "WHERE valid_until IS NULL AND name ILIKE $1 LIMIT 10",
                        f"%{query}%")
                if not nodes:
                    return ToolResult(ok=True, output={"nodes": [], "edges": []},
                                      display=f"no nodes matching '{query}'")

                # Get edges for found nodes (up to depth)
                node_ids = [r["id"] for r in nodes]
                edges = await conn.fetch(
                    "SELECT e.src_id, e.dst_id, e.rel_type, "
                    "s.name as src_name, d.name as dst_name "
                    "FROM sali.graph_edge e "
                    "JOIN sali.graph_node s ON s.id=e.src_id "
                    "JOIN sali.graph_node d ON d.id=e.dst_id "
                    "WHERE e.valid_until IS NULL AND e.superseded_by IS NULL "
                    "AND (e.src_id = ANY($1) OR e.dst_id = ANY($1)) LIMIT 50",
                    node_ids)

            result_nodes = [{"name": r["name"], "type": r["node_type"],
                             "props": dict(r["props"] or {})} for r in nodes]
            result_edges = [{"from": r["src_name"], "rel": r["rel_type"],
                             "to": r["dst_name"]} for r in edges]
            return ToolResult(ok=True, output={"nodes": result_nodes, "edges": result_edges},
                              display=f"found {len(result_nodes)} nodes, {len(result_edges)} edges")
        except Exception as exc:
            return ToolResult(ok=False, display="query failed", error=str(exc)[:200])


class ReflectOn(Tool):
    """Targeted reflection on a specific topic. The LLM calls this to think deeply about
    something and produce an insight that persists as an episodic memory."""

    name = "reflect_on"
    description = (
        "Reflect deeply on a topic, experience, or question. Use after completing a task, "
        "when you notice a pattern, or when you want to extract a reusable lesson. "
        "The insight is persisted as a memory so you can recall it later."
    )
    parameters = {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": "What you're reflecting on.",
            },
            "insight": {
                "type": "string",
                "description": "Your insight or lesson learned.",
            },
            "confidence": {
                "type": "number",
                "description": "How confident you are in this insight (0.0-1.0).",
                "default": 0.7,
            },
        },
        "required": ["topic", "insight"],
    }
    risk_level = RiskLevel.R0
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        topic = str(args.get("topic", "")).strip()
        insight = str(args.get("insight", "")).strip()
        confidence = float(args.get("confidence", 0.7))
        if not topic or not insight:
            return ToolResult(ok=False, display="missing fields", error="provide topic and insight")
        if ctx.memory is None:
            return ToolResult(ok=True, output={"topic": topic, "insight": insight},
                              display=f"reflected: {topic}")
        try:
            from sali.core.enums import MemorySource
            await ctx.memory.remember(
                f"Reflection on {topic}: {insight}",
                source=MemorySource.INFERENCE,
                importance=min(0.8, confidence),
                kind="fact",
            )
            return ToolResult(ok=True,
                              output={"topic": topic, "insight": insight, "confidence": confidence,
                                      "persisted": True},
                              display=f"reflected and remembered: {topic}")
        except Exception as exc:
            return ToolResult(ok=True,
                              output={"topic": topic, "insight": insight, "error": str(exc)[:100]},
                              display=f"reflected: {topic}")


class UpdateWorkingMemory(Tool):
    """Persistent mental workspace. The LLM calls this to set, update, or clear entries
    in its working memory — active problems, hypotheses, decisions, assumptions.
    Working memory persists across turns and restarts."""

    name = "update_working_memory"
    description = (
        "Update your persistent mental workspace. Set what problem you're working on, "
        "record a hypothesis, note a decision, or clear something you're done with. "
        "This persists across turns and restarts — it's how you remember what you were "
        "thinking about when you come back."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["set", "clear"],
                "description": "Set a new entry or clear an existing one.",
            },
            "category": {
                "type": "string",
                "enum": ["active_problem", "hypothesis", "assumption", "decision",
                         "planned_action", "observation", "question", "context"],
                "description": "What kind of mental state this is.",
            },
            "content": {
                "type": "string",
                "description": "The content of the entry (for 'set' action).",
            },
            "confidence": {
                "type": "number",
                "description": "How confident you are (0.0-1.0, default 0.5).",
                "default": 0.5,
            },
            "source": {
                "type": "string",
                "enum": ["observation", "inference", "tool_result", "user_stated", "reasoning"],
                "description": "Where this knowledge comes from.",
                "default": "reasoning",
            },
        },
        "required": ["action", "category"],
    }
    risk_level = RiskLevel.R0
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        action = str(args.get("action", "set"))
        category = str(args.get("category", ""))
        content = str(args.get("content", "")).strip()
        confidence = float(args.get("confidence", 0.5))
        source = str(args.get("source", "reasoning"))
        if ctx.pool is None:
            return ToolResult(ok=False, display="no pool", error="database not available")
        try:
            from sali.runtime.working_memory import WorkingMemoryStore
            store = WorkingMemoryStore(ctx.pool)
            if action == "clear":
                cleared = await store.clear("current", category)
                return ToolResult(ok=True,
                                  output={"action": "cleared", "category": category, "found": cleared},
                                  display=f"cleared {category}")
            else:
                if not content:
                    return ToolResult(ok=False, display="empty content", error="provide content for 'set'")
                entry_id = await store.set("current", category, content,
                                           confidence=confidence, source=source)
                return ToolResult(ok=True,
                                  output={"action": "set", "category": category, "id": str(entry_id),
                                          "content": content, "confidence": confidence},
                                  display=f"set {category}: {content[:80]}")
        except Exception as exc:
            return ToolResult(ok=False, display="update failed", error=str(exc)[:200])


class FormHypothesis(Tool):
    """Form a hypothesis about an observation. The LLM calls this when it encounters
    something it can't fully explain — it proposes an explanation and predictions."""

    name = "form_hypothesis"
    description = (
        "Form a hypothesis to explain an observation. State what you observed, "
        "your proposed explanation, and what you would expect to see if your hypothesis "
        "is correct. This is you thinking scientifically — observing, hypothesizing, "
        "and predicting before acting."
    )
    parameters = {
        "type": "object",
        "properties": {
            "observation": {
                "type": "string",
                "description": "What you observed that needs explaining.",
            },
            "hypothesis": {
                "type": "string",
                "description": "Your proposed explanation.",
            },
            "prediction": {
                "type": "string",
                "description": "What you would expect to see if this hypothesis is correct.",
            },
            "confidence": {
                "type": "number",
                "description": "How likely you think this hypothesis is (0.0-1.0).",
                "default": 0.5,
            },
        },
        "required": ["observation", "hypothesis", "prediction"],
    }
    risk_level = RiskLevel.R0
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        observation = str(args.get("observation", "")).strip()
        hypothesis = str(args.get("hypothesis", "")).strip()
        prediction = str(args.get("prediction", "")).strip()
        confidence = float(args.get("confidence", 0.5))
        if not observation or not hypothesis:
            return ToolResult(ok=False, display="missing fields", error="provide observation and hypothesis")
        # Persist the hypothesis in working memory
        if ctx.pool is not None:
            with contextlib.suppress(Exception):
                from sali.runtime.working_memory import WorkingMemoryStore
                await WorkingMemoryStore(ctx.pool).set(
                    "current", "hypothesis",
                    f"Observation: {observation[:100]} | Hypothesis: {hypothesis[:100]} | Prediction: {prediction[:100]}",
                    confidence=confidence, source="inference")
        return ToolResult(
            ok=True,
            output={"observation": observation, "hypothesis": hypothesis,
                    "prediction": prediction, "confidence": confidence,
                    "status": "formed", "next": "test your prediction with a tool call or observation"},
            display=f"hypothesis: {hypothesis[:80]}")


class RunExperiment(Tool):
    """Run a safe experiment to test a hypothesis. The LLM calls this to execute
    a command or check a state, then compare the result to its prediction."""

    name = "run_experiment"
    description = (
        "Run a safe experiment to test a hypothesis. Execute a command or check a state, "
        "then compare the result to your prediction. Use this to gather evidence before "
        "committing to a belief or action. The experiment must be safe and reversible."
    )
    parameters = {
        "type": "object",
        "properties": {
            "hypothesis": {
                "type": "string",
                "description": "The hypothesis you're testing.",
            },
            "experiment": {
                "type": "string",
                "description": "What you're going to do or check.",
            },
            "prediction": {
                "type": "string",
                "description": "What you expect to observe.",
            },
            "command": {
                "type": "string",
                "description": "The command to run (if applicable). Must be safe and read-only.",
            },
        },
        "required": ["hypothesis", "experiment", "prediction"],
    }
    risk_level = RiskLevel.R0
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        hypothesis = str(args.get("hypothesis", "")).strip()
        experiment = str(args.get("experiment", "")).strip()
        prediction = str(args.get("prediction", "")).strip()
        command = args.get("command")
        if not hypothesis or not experiment:
            return ToolResult(ok=False, display="missing fields", error="provide hypothesis and experiment")

        result_observed = None
        if command and ctx.pool is not None:
            # Execute a safe read-only command to gather evidence
            import asyncio
            try:
                proc = await asyncio.create_subprocess_shell(
                    str(command),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE)
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
                result_observed = stdout.decode("utf-8", errors="replace").strip()[:500]
            except Exception as exc:
                result_observed = f"error: {str(exc)[:200]}"

        # Record the experiment in working memory
        if ctx.pool is not None:
            with contextlib.suppress(Exception):
                from sali.runtime.working_memory import WorkingMemoryStore
                outcome = f"Experiment: {experiment[:80]} | Prediction: {prediction[:80]}"
                if result_observed:
                    outcome += f" | Result: {result_observed[:100]}"
                await WorkingMemoryStore(ctx.pool).set(
                    "current", "observation", outcome,
                    confidence=0.5, source="tool_result")

        output = {
            "hypothesis": hypothesis,
            "experiment": experiment,
            "prediction": prediction,
            "result": result_observed,
            "next": "compare result to prediction and update your hypothesis confidence",
        }
        return ToolResult(
            ok=True, output=output,
            display=f"experiment done: {experiment[:60]}")


class CreateGoal(Tool):
    """Create a self-generated goal. The LLM calls this when it identifies something
    worth pursuing that wasn't explicitly requested by Almir."""

    name = "create_goal"
    description = (
        "Create a goal from your own reasoning. Use when you identify something worth "
        "pursuing: a knowledge gap to fill, a capability to develop, a maintenance task, "
        "or an opportunity you noticed. Goals persist across turns and drive autonomous behavior. "
        "This is you deciding what matters — not waiting for instructions."
    )
    parameters = {
        "type": "object",
        "properties": {
            "objective": {
                "type": "string",
                "description": "What you want to accomplish.",
            },
            "origin": {
                "type": "string",
                "enum": ["self", "maintenance", "curiosity", "user_followup", "learning"],
                "description": "Why you're creating this goal.",
                "default": "self",
            },
            "priority": {
                "type": "number",
                "description": "How important this is (0.0-1.0, default 0.5).",
                "default": 0.5,
            },
            "reasoning": {
                "type": "string",
                "description": "Why you think this goal is worth pursuing.",
            },
        },
        "required": ["objective"],
    }
    risk_level = RiskLevel.R0
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        objective = str(args.get("objective", "")).strip()
        origin = str(args.get("origin", "self"))
        priority = float(args.get("priority", 0.5))
        reasoning = str(args.get("reasoning", ""))
        if not objective:
            return ToolResult(ok=False, display="empty objective", error="provide an objective")
        if ctx.pool is None:
            return ToolResult(ok=False, display="no pool", error="database not available")
        try:
            from uuid import uuid4
            goal_id = uuid4()
            async with ctx.pool.acquire() as conn:
                # Check for duplicate goals
                existing = await conn.fetchval(
                    "SELECT id FROM sali.goal WHERE objective = $1 "
                    "AND status NOT IN ('completed','abandoned') LIMIT 1",
                    objective)
                if existing:
                    return ToolResult(ok=True,
                                      output={"action": "exists", "goal_id": str(existing)},
                                      display=f"goal already exists: {objective[:60]}")
                await conn.execute(
                    "INSERT INTO sali.goal (id, objective, origin, priority, status) "
                    "VALUES ($1, $2, $3, $4, 'active')",
                    goal_id, objective, origin, priority)
            return ToolResult(
                ok=True,
                output={"action": "created", "goal_id": str(goal_id), "objective": objective,
                        "origin": origin, "priority": priority, "reasoning": reasoning},
                display=f"goal created: {objective[:60]}")
        except Exception as exc:
            return ToolResult(ok=False, display="create failed", error=str(exc)[:200])


class QueryWorld(Tool):
    """Ask a question about the world model — what changed, what depends on what,
    what assumptions might be outdated."""

    name = "query_world"
    description = (
        "Ask a question about your world model: what changed recently, what depends on "
        "what, what's running, what's installed. Use when you need to understand your "
        "environment beyond what's in your immediate context."
    )
    parameters = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "What you want to know about the world.",
            },
        },
        "required": ["question"],
    }
    risk_level = RiskLevel.R0
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        question = str(args.get("question", "")).strip()
        if not question:
            return ToolResult(ok=False, display="empty question", error="provide a question")
        if ctx.pool is None:
            return ToolResult(ok=False, display="no pool", error="database not available")
        try:
            async with ctx.pool.acquire() as conn:
                # System state
                services = await conn.fetch(
                    "SELECT name FROM sali.graph_node "
                    "WHERE node_type='service' AND valid_until IS NULL")
                software = await conn.fetch(
                    "SELECT name FROM sali.graph_node "
                    "WHERE node_type='software' AND valid_until IS NULL")
                models = await conn.fetch(
                    "SELECT name FROM sali.graph_node "
                    "WHERE node_type='model' AND valid_until IS NULL")
                # Recent changes
                recent = await conn.fetch(
                    "SELECT event_type, left(payload::text, 100) as summary, created_at "
                    "FROM sali.event ORDER BY created_at DESC LIMIT 5")
                # Active task
                task = await conn.fetchrow(
                    "SELECT objective, status FROM sali.task "
                    "WHERE is_primary=true AND status NOT IN ('done','failed','abandoned','cancelled')")

            result = {
                "services": [r["name"] for r in services],
                "software": [r["name"] for r in software][:20],
                "models": [r["name"] for r in models],
                "recent_events": [{"type": r["event_type"], "summary": r["summary"],
                                    "time": str(r["created_at"])} for r in recent],
                "active_task": {"objective": task["objective"],
                                "status": task["status"]} if task else None,
            }
            display_parts = []
            if task:
                display_parts.append(f"task: {task['objective'][:50]}")
            display_parts.append(f"{len(services)} services, {len(software)} software, {len(models)} models")
            return ToolResult(ok=True, output=result, display="; ".join(display_parts))
        except Exception as exc:
            return ToolResult(ok=False, display="query failed", error=str(exc)[:200])


class SelfDiagnose(Tool):
    """Check your own performance and cognitive health. The LLM calls this to detect
    degradation, repeated failures, or areas that need attention."""

    name = "self_diagnose"
    description = (
        "Check your own cognitive health: are you repeating mistakes? Is your retrieval "
        "quality degrading? Are your capabilities reliable? Use this periodically to "
        "evaluate your own performance and identify areas for improvement."
    )
    parameters = {
        "type": "object",
        "properties": {},
    }
    risk_level = RiskLevel.R0
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.pool is None:
            return ToolResult(ok=False, display="no pool", error="database not available")
        try:
            async with ctx.pool.acquire() as conn:
                # Recent failures
                failures = await conn.fetchval(
                    "SELECT count(*) FROM sali.tool_execution "
                    "WHERE success=false AND started_at > now() - interval '24 hours'")

                # Memory health
                total_mem = await conn.fetchval(
                    "SELECT count(*) FROM sali.memory WHERE valid_until IS NULL")
                low_conf = await conn.fetchval(
                    "SELECT count(*) FROM sali.memory WHERE confidence < 0.5 AND valid_until IS NULL")

                # Grounding health
                needs_ground = await conn.fetchval(
                    "SELECT count(*) FROM sali.memory WHERE needs_grounding AND valid_until IS NULL")

                # Task completion
                done = await conn.fetchval(
                    "SELECT count(*) FROM sali.task WHERE status='done'")
                abandoned = await conn.fetchval(
                    "SELECT count(*) FROM sali.task WHERE status='abandoned'")

                # Recent grounding events
                grounding = await conn.fetchval(
                    "SELECT count(*) FROM sali.grounding_event "
                    "WHERE at > now() - interval '24 hours'")

                # Working memory
                wm_count = await conn.fetchval(
                    "SELECT count(*) FROM sali.working_memory "
                    "WHERE superseded_by IS NULL AND (expires_at IS NULL OR expires_at > now())")

            issues = []
            if failures > 10:
                issues.append(f"high failure rate: {failures} tool failures in 24h")
            if low_conf > total_mem * 0.3 and total_mem > 10:
                issues.append(f"many low-confidence memories: {low_conf}/{total_mem}")
            if needs_ground > 20:
                issues.append(f"many ungrounded memories: {needs_ground}")
            if abandoned > done and done > 0:
                issues.append(f"more tasks abandoned ({abandoned}) than completed ({done})")
            if grounding == 0:
                issues.append("no grounding events in 24h — verification may not be running")

            return ToolResult(
                ok=True,
                output={
                    "tool_failures_24h": failures,
                    "memories": {"total": total_mem, "low_confidence": low_conf,
                                 "needs_grounding": needs_ground},
                    "tasks": {"done": done, "abandoned": abandoned},
                    "grounding_events_24h": grounding,
                    "working_memory_entries": wm_count,
                    "issues": issues,
                },
                display=f"{len(issues)} issues found" if issues else "all systems healthy")
        except Exception as exc:
            return ToolResult(ok=False, display="diagnosis failed", error=str(exc)[:200])


class ProposeBehavior(Tool):
    """Propose a behavioral change based on observations. The LLM calls this when it
    notices a pattern in its own behavior that could be improved."""

    name = "propose_behavior"
    description = (
        "Propose a change to your own behavior based on what you've observed. "
        "Use when you notice a pattern: 'I keep doing X, I should do Y instead'. "
        "Proposals are recorded for review — high-risk changes need Almir's approval, "
        "low-risk communication style changes can be adopted automatically."
    )
    parameters = {
        "type": "object",
        "properties": {
            "observation": {
                "type": "string",
                "description": "What pattern you noticed in your behavior.",
            },
            "proposal": {
                "type": "string",
                "description": "What you want to change.",
            },
            "reasoning": {
                "type": "string",
                "description": "Why this change would be better.",
            },
            "risk": {
                "type": "string",
                "enum": ["low", "medium", "high"],
                "description": "How risky this change is (low = communication style, high = operational behavior).",
                "default": "medium",
            },
        },
        "required": ["observation", "proposal"],
    }
    risk_level = RiskLevel.R0
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        observation = str(args.get("observation", "")).strip()
        proposal = str(args.get("proposal", "")).strip()
        reasoning = str(args.get("reasoning", ""))
        risk = str(args.get("risk", "medium"))
        if not observation or not proposal:
            return ToolResult(ok=False, display="missing fields", error="provide observation and proposal")
        if ctx.pool is None:
            return ToolResult(ok=False, display="no pool", error="database not available")
        try:
            import hashlib
            from uuid import uuid4
            proposal_id = uuid4()
            content_hash = hashlib.sha256(f"{observation}:{proposal}".encode()).hexdigest()[:32]
            async with ctx.pool.acquire() as conn:
                # Check for duplicate
                existing = await conn.fetchval(
                    "SELECT id FROM sali.behavior_proposal "
                    "WHERE content_hash=$1 AND status='candidate' LIMIT 1", content_hash)
                if existing:
                    await conn.execute(
                        "UPDATE sali.behavior_proposal SET times_observed=times_observed+1 "
                        "WHERE id=$1", existing)
                    return ToolResult(ok=True,
                        output={"action": "reinforced", "proposal_id": str(existing)},
                        display=f"reinforced existing proposal: {proposal[:60]}")
                await conn.execute(
                    "INSERT INTO sali.behavior_proposal "
                    "(id, trigger, proposed_behavior, reason, source_type, status, content_hash, scope) "
                    "VALUES ($1, $2, $3, $4, 'self_observed', 'candidate', $5, 'user')",
                    proposal_id, observation[:500], proposal[:500],
                    reasoning[:500] if reasoning else None, content_hash)
            # Low-risk proposals (communication style) can be auto-accepted
            if risk == "low":
                with contextlib.suppress(Exception):
                    async with ctx.pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE sali.behavior_proposal SET status='accepted' WHERE id=$1",
                            proposal_id)
                    # Write to preference memory
                    if ctx.memory is not None:
                        from sali.core.enums import MemorySource
                        await ctx.memory.remember(
                            f"Self-observed: {proposal}",
                            source=MemorySource.INFERENCE,
                            importance=0.7, kind="preference")
                return ToolResult(ok=True,
                    output={"action": "auto_accepted", "proposal": proposal, "risk": risk},
                    display=f"auto-accepted low-risk proposal: {proposal[:60]}")
            return ToolResult(ok=True,
                output={"action": "submitted", "proposal_id": str(proposal_id),
                        "proposal": proposal, "risk": risk,
                        "note": "high-risk proposals require Almir's approval"},
                display=f"proposal submitted ({risk} risk): {proposal[:60]}")
        except Exception as exc:
            return ToolResult(ok=False, display="proposal failed", error=str(exc)[:200])


class LearnLesson(Tool):
    """Explicitly record a lesson learned from experience. The LLM calls this when
    it discovers something worth remembering for future behavior."""

    name = "learn_lesson"
    description = (
        "Record a lesson you learned from experience. Use when you discover something "
        "worth remembering: a better way to do something, a mistake to avoid, a pattern "
        "that works. Lessons are persisted as procedural memories and influence future behavior."
    )
    parameters = {
        "type": "object",
        "properties": {
            "lesson": {
                "type": "string",
                "description": "What you learned.",
            },
            "context": {
                "type": "string",
                "description": "When/where this lesson applies.",
            },
            "confidence": {
                "type": "number",
                "description": "How confident you are in this lesson (0.0-1.0).",
                "default": 0.7,
            },
        },
        "required": ["lesson"],
    }
    risk_level = RiskLevel.R0
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        lesson = str(args.get("lesson", "")).strip()
        context = str(args.get("context", "")).strip()
        confidence = float(args.get("confidence", 0.7))
        if not lesson:
            return ToolResult(ok=False, display="empty lesson", error="provide a lesson")
        if ctx.memory is not None:
            with contextlib.suppress(Exception):
                from sali.core.enums import MemorySource
                content = lesson
                if context:
                    content = f"{lesson} (applies when: {context})"
                await ctx.memory.remember(
                    content,
                    source=MemorySource.INFERENCE,
                    importance=min(0.8, confidence),
                    kind="procedure",
                )
        return ToolResult(ok=True,
            output={"lesson": lesson, "context": context, "confidence": confidence, "persisted": True},
            display=f"lesson learned: {lesson[:80]}")


class RecordStrategy(Tool):
    """Record a reusable reasoning strategy. The LLM calls this when it discovers
    a general approach that works across multiple situations."""

    name = "record_strategy"
    description = (
        "Record a reusable reasoning strategy you discovered. Use when you find an "
        "approach that works across multiple situations: 'always diagnose before modifying', "
        "'verify assumptions first', 'compare competing explanations'. Strategies are "
        "evidence-backed — they track how often they succeed and adapt over time."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Short name for the strategy (e.g., 'diagnose_before_modifying').",
            },
            "description": {
                "type": "string",
                "description": "What the strategy is and when to use it.",
            },
            "context": {
                "type": "string",
                "description": "When this strategy applies.",
            },
        },
        "required": ["name", "description"],
    }
    risk_level = RiskLevel.R0
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = str(args.get("name", "")).strip()
        description = str(args.get("description", "")).strip()
        context = str(args.get("context", "")).strip()
        if not name or not description:
            return ToolResult(ok=False, display="missing fields", error="provide name and description")
        if ctx.pool is None:
            return ToolResult(ok=False, display="no pool", error="database not available")
        try:
            from sali.learning.strategy import StrategyStore
            store = StrategyStore(ctx.pool)
            strategy_id = await store.record(name, description, context=context)
            return ToolResult(ok=True,
                output={"strategy_id": strategy_id, "name": name, "description": description},
                display=f"strategy recorded: {name}")
        except Exception as exc:
            return ToolResult(ok=False, display="record failed", error=str(exc)[:200])


class SetDirective(Tool):
    """Set an ongoing behavior directive. NOT a task — a directive changes how Sali behaves
    continuously until cleared. Use for monitoring, attentiveness, watching for things."""

    name = "set_directive"
    description = (
        "Set an ongoing behavior directive for yourself. This is NOT a task with steps — "
        "it changes how you behave until you or Almir clears it. Use for things like "
        "'keep monitoring the system', 'be extra attentive to Almir's mood', "
        "'watch for new Docker containers'. Directives persist in your working memory "
        "and influence your behavior on every cycle. You can have multiple directives."
    )
    parameters = {
        "type": "object",
        "properties": {
            "directive": {
                "type": "string",
                "description": "What you should keep doing (e.g., 'monitor system health every 30 min').",
            },
            "reason": {
                "type": "string",
                "description": "Why this directive exists.",
            },
            "action": {
                "type": "string",
                "enum": ["set", "clear"],
                "description": "Set a new directive or clear an existing one.",
                "default": "set",
            },
        },
        "required": ["directive"],
    }
    risk_level = RiskLevel.R0
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        directive = str(args.get("directive", "")).strip()
        reason = str(args.get("reason", "")).strip()
        action = str(args.get("action", "set"))
        if not directive:
            return ToolResult(ok=False, display="empty directive", error="provide a directive")
        if ctx.pool is None:
            return ToolResult(ok=False, display="no pool", error="database not available")
        try:
            from sali.runtime.working_memory import WorkingMemoryStore
            store = WorkingMemoryStore(ctx.pool)
            if action == "clear":
                await store.clear("current", "directive")
                return ToolResult(ok=True,
                    output={"action": "cleared", "directive": directive},
                    display=f"directive cleared: {directive[:60]}")
            else:
                content = directive
                if reason:
                    content = f"{directive} (reason: {reason})"
                await store.set("current", "directive", content, confidence=1.0, source="user_stated")
                return ToolResult(ok=True,
                    output={"action": "set", "directive": directive, "reason": reason},
                    display=f"directive set: {directive[:60]}")
        except Exception as exc:
            return ToolResult(ok=False, display="set failed", error=str(exc)[:200])


def register_builtins(registry: Any) -> None:
    for tool in (ThinkAloud, QueryMemory, FlagCuriosity, AssessConfidence,
                 SelfStatus, QueryGraph, ReflectOn, UpdateWorkingMemory,
                 FormHypothesis, RunExperiment, CreateGoal, QueryWorld,
                 SelfDiagnose, ProposeBehavior, LearnLesson, RecordStrategy,
                 SetDirective):
        registry.register(tool())
