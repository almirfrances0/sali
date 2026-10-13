# Sali Cognitive Architecture — The Persistent Digital Person

**Status:** Phase 0 — Architecture Specification  
**Date:** 2026-09-15  
**Constraint:** iOS app unchanged. API/WS contract backward-compatible. No rewrite. Incremental refactor.

---

## 0. Core Principle

**Sali is not an LLM. Sali is a persistent digital entity that uses an LLM as its reasoning substrate.**

```
SALI = identity + memory + goals + experience + world model + self model
       + relationships + capabilities + strategies + continuity + attention
       + hypotheses + reflection + learning + body + safety + time

LLM = replaceable reasoning engine called by Sali's cognitive executive
```

The LLM can be swapped (Qwen → Llama → GPT → whatever comes next) without destroying Sali. Identity, memory, goals, and experience live in PostgreSQL — not in model weights or prompt strings.

---

## 1. Digital Person

### What Sali Is

Sali is a single persistent digital entity that:
- Exists continuously from boot to shutdown (and resumes after restart)
- Has a stable identity (name, pronouns, role, owner, relationships)
- Maintains a continuous memory of its existence
- Forms and pursues goals
- Learns from experience
- Models the world and the people in it
- Makes decisions based on accumulated knowledge
- Can initiate action without external prompting

### What Sali Is Not

- Not a chatbot waiting for prompts
- Not a collection of scheduled background jobs
- Not a prompt template with tools attached
- Not a simulation of consciousness
- Not an entity whose identity depends on a specific model

### Identity

Identity is **durable state in PostgreSQL**, not a prompt string.

| Attribute | Source | Survives restart | Survives model swap |
|-----------|--------|-----------------|-------------------|
| Name | `sali.person` + graph | Yes | Yes |
| Pronouns | graph `agent:sali` props | Yes | Yes |
| Role | graph `agent:sali` props | Yes | Yes |
| Owner | `sali.person` (relationship_type=owner) | Yes | Yes |
| Self-knowledge | `self_state.py` `_compose_self_knowledge()` | Yes | Yes |
| Life history | `sali.memory` (layer=identity) | Yes | Yes |

The `IDENTITY` constant in `context/engine.py` is the **delivery format** — how identity reaches the LLM. It is NOT the source of truth. The source of truth is the database.

### Current state (verified):
- `sali.person`: 1 row (Almir, owner, explicit, confidence=1.0)
- `sali.graph_node`: `agent:sali` with props (presentation, pronouns, role, owner)
- `sali.memory`: 2 identity rows (self-knowledge + protected safety rule)
- `IDENTITY` constant: updated with Ollama-brain connection, proactive communication guidance

---

## 2. Cognitive Executive

### Current Architecture (FSM-driven)

```
User message → FSM → retrieve → build_context → LLM → tools → verify → respond
```

The FSM (`runtime/state.py`) dictates every step. The LLM is called at exactly one site (`loop.py:3036`). The LLM receives pre-assembled context and generates tokens. Tool calls are dispatched by the FSM.

### Target Architecture (LLM-driven)

```
Sali perceives → cognitive executive decides → LLM reasons → tools execute → verify → learn → continue
```

The cognitive executive is a **new layer** between the FSM and the LLM. It decides:
- Whether cognition is needed (not every perception requires thought)
- What information to retrieve
- What to think about
- What actions to take
- When to reflect
- When to learn
- When to rest

### Cognitive Executive Interface

```python
class CognitiveExecutive:
    """The decision-making core. Not the LLM — the thing that decides WHEN and HOW to use the LLM."""
    
    async def perceive(self, world_state: WorldState) -> AttentionSignal:
        """What deserves attention right now?"""
    
    async def decide(self, attention: AttentionSignal, state: CognitiveState) -> CognitiveDecision:
        """What should Sali do? Think? Act? Rest? Learn?"""
    
    async def think(self, decision: CognitiveDecision, context: AssembledContext) -> ThoughtResult:
        """Use the LLM to reason about the decision. Returns structured reasoning, not just text."""
    
    async def act(self, thought: ThoughtResult) -> ActionResult:
        """Execute the decided action through the body (tools, API, etc.)."""
    
    async def learn(self, action: ActionResult, observation: Any) -> LearningOutcome:
        """What did Sali learn from this action? Update memory, strategies, capabilities."""
    
    async def reflect(self, state: CognitiveState) -> ReflectionOutcome:
        """Analyze recent experience for patterns, lessons, strategy improvements."""
```

### Key Design Decision

The executive does NOT call the LLM directly. It calls `think()` which internally uses the LLM. This separation means:
- The executive is deterministic (decisions are auditable)
- The LLM is replaceable (only `think()` changes)
- Cheap decisions (perception, attention) don't require LLM calls
- Expensive reasoning is bounded and intentional

### Files to modify:
- `src/sali/runtime/loop.py` — extract cognitive decision points from the FSM
- NEW: `src/sali/runtime/executive.py` — the CognitiveExecutive class
- `src/sali/runtime/cognitive.py` — extend CognitiveState with executive decisions

### Files to reuse unchanged:
- `src/sali/runtime/state.py` — FSM states remain as the body's state machine
- `src/sali/runtime/coordinator.py` — single cognition lock stays
- `src/sali/runtime/lease.py` — cross-process coordination stays

---

## 3. Attention

### Current State

Attention is reactive: user message → classify → route. The `InitiativeEngine` scans durable state on a 120-second interval and generates candidates. There is no explicit attention ranking.

### Target Architecture

Attention is a **first-class faculty** that ranks competing demands for cognitive resources.

```python
@dataclass
class AttentionCandidate:
    source: str          # 'user_message' | 'goal' | 'failure' | 'curiosity' | 'environment' | 'commitment' | 'anomaly'
    urgency: float       # 0.0-1.0
    importance: float    # 0.0-1.0
    age_seconds: float   # how long this has been waiting
    evidence_strength: float  # 0.0-1.0
    cost_estimate: float      # expected cognitive cost
    value_estimate: float     # expected value of attending to this
```

**Attention sources:**
- `user_message` — always highest priority (urgency=1.0)
- `commitment` — overdue commitments (urgency=0.8)
- `goal` — active goals with next actions (urgency=0.6)
- `failure` — recent tool failures or contradictions (urgency=0.7)
- `curiosity` — open knowledge gaps (urgency=0.3)
- `environment` — machine state changes (urgency=0.4)
- `anomaly` — unexpected patterns (urgency=0.5)

**Attention scoring:**
```
score = 0.4 * urgency + 0.3 * importance + 0.2 * (1.0 - cost_estimate) + 0.1 * evidence_strength
```

**Files to modify:**
- `src/sali/runtime/attention.py` — extend with AttentionCandidate and scoring
- `src/sali/cognitive/initiative_driver.py` — feed into attention instead of directly creating candidates

**Files to reuse:**
- `src/sali/runtime/initiative.py` — InitiativeEngine becomes a source of attention candidates
- `src/sali/events/proactive.py` — proactive loop continues as-is

---

## 4. Working Memory

### Current State

Implemented: `src/sali/runtime/working_memory.py` with `WorkingMemoryStore`. Categories: active_problem, hypothesis, assumption, decision, planned_action, observation, question, context. Injected as P1 context section.

### Target Architecture

Working memory is Sali's **persistent mental workspace**. It must:
- Survive context compaction (already does)
- Survive process restart (already does)
- Be explicitly managed by the cognitive executive (partially — `update_working_memory` tool exists)
- Drive attention (not yet — working memory entries should generate attention candidates)
- Support interruption/resume (partially — the Task State Capsule handles task state, but cognitive state like "I was investigating X" is lost)

### Extensions needed:
1. **Attention integration**: working memory entries with category `active_problem` or `hypothesis` should generate attention candidates
2. **Interruption recovery**: when Sali restarts, working memory entries should be injected into the first turn's context
3. **Cognitive state persistence**: the executive's current reasoning state (what it was thinking about, what step it was on) should be persisted

### Files to modify:
- `src/sali/runtime/working_memory.py` — add `attention_candidates()` method
- `src/sali/runtime/loop.py` — inject working memory on restart
- NEW: `src/sali/runtime/cognitive_state_persistence.py` — persist executive state

---

## 5. Long-Term Memory

### Current State

Five layers: semantic, episodic, procedural, identity, preference, system_env. Hybrid retrieval (vector + keyword). Per-layer quotas and dampeners. Claim-key supersession. Evidence-count corroboration.

### Target Architecture

Seven layers (add strategy and relational):

| Layer | Purpose | Examples |
|-------|---------|---------|
| identity | Who Sali is | "I am Sali", "I live on this machine" |
| preference | Almir's preferences | "Almir prefers dark theme", "Call me Sir" |
| semantic | World facts | "Docker is installed", "Almir runs hirecyberz.com" |
| episodic | Experiences | "I built a website for NovaTech", "Almir corrected me about disk size" |
| procedural | How to do things | "How to build a Laravel site", "How to check GPU status" |
| strategy | Reusable reasoning approaches | "Diagnose before modifying", "Verify assumptions first" |
| system_env | Machine inventory | "Software installed: Docker, Git, Node.js" |

### Active Memory

Currently: retrieval router decides what to retrieve deterministically. LLM has no influence.

Target: LLM can issue deliberate retrieval queries during reasoning via `query_memory` tool. The deterministic router stays as the default, but the LLM can override it.

### Files to modify:
- `src/sali/retrieval/service.py` — add `deliberate_retrieve()` method that bypasses the router
- `src/sali/tools/builtins/cognitive_tool.py` — `query_memory` already exists, wire it to `deliberate_retrieve`

### Files to reuse:
- `src/sali/memory/service.py` — scoring and retrieval pipeline unchanged
- `src/sali/memory/writer.py` — write path unchanged
- `src/sali/memory/retriever.py` — SQL retrievers unchanged

---

## 6. World Model

### Current State

The knowledge graph (`sali.graph_node` + `sali.graph_edge`) contains 3,326 nodes (mostly ext_tool). Temporal validity `[valid_from, valid_until)`. Contradiction detection via `set_fact()`.

### Target Architecture

The graph becomes Sali's **active world model** — not just a memory store, but a model that:
- Is continuously updated by the perception engine (already does this via twin refresh)
- Can be queried during reasoning (partially — `query_graph` tool exists)
- Supports temporal queries ("what changed since X")
- Tracks assumptions and their evidence
- Detects contradictions and triggers investigation

### Extensions needed:
1. **Temporal queries**: "what changed in the graph since last week"
2. **Assumption tracking**: graph edges should carry `assumption: bool` and `evidence_strength: float`
3. **Active investigation**: when a contradiction is detected, the executive should investigate

### Files to modify:
- `src/sali/graph/service.py` — add temporal query methods
- `src/sali/tools/builtins/cognitive_tool.py` — extend `query_graph` with temporal queries

---

## 7. Self Model

### Current State

`CognitiveState` in `runtime/cognitive.py` assembles a 40+ field snapshot from PostgreSQL. Injected into context as `self_note`. Not directly accessible to the LLM as a tool.

### Target Architecture

The self model is Sali's **persistent knowledge about herself**:
- What she knows (memory counts, knowledge gaps)
- What she doesn't know (curiosities, low-confidence beliefs)
- What she can do (capabilities, tools)
- What she's doing (active task, working memory)
- How she's performing (grounding metrics, success rate)
- What she's learned (strategies, procedures, lessons)

### Extensions needed:
1. **`self_status` tool** — already exists, returns full cognitive state
2. **Performance metrics** — track success rate, prediction accuracy, strategy effectiveness
3. **Capability confidence** — each capability has a confidence score based on evidence

### Files to modify:
- `src/sali/runtime/cognitive.py` — add performance metrics to CognitiveState
- `src/sali/tools/builtins/cognitive_tool.py` — `self_status` already exists

---

## 8. Almir Model

### Current State

- `sali.person` table: 1 row (Almir, owner)
- `sali/memory` layer=preference: 2 rows
- `runtime/almir_model.py`: derives emotional state from recent messages
- Injected as P1 context section (`almir_state_note`)

### Target Architecture

An evolving model of Almir that is:
- Evidence-based (observed > inferred > believed)
- Temporal (preferences change over time)
- Contextual (Almir behaves differently in different situations)
- Private (never shared or leaked)

### Extensions needed:
1. **Preference change tracking**: when Almir says "I don't want to sleep now" after previously wanting reminders, track the change
2. **Communication pattern recognition**: how often Almir messages, what times, what topics
3. **Emotional state persistence**: the derived emotional state should persist across turns

### Files to modify:
- `src/sali/runtime/almir_model.py` — add persistence and change tracking
- `src/sali/people/store.py` — extend with preference history

---

## 9. Goal System

### Current State

- `sali.goal` table exists with constraints (origin, status, priority)
- `create_goal` cognitive tool exists
- InitiativeEngine scans goals as one of 7 sources
- No autonomous goal generation from the LLM

### Target Architecture

Goals have:
- **Origin**: user, self_initiated, maintenance, curiosity, learning, commitment
- **Reason**: grounded justification for the goal
- **Priority**: 0.0-1.0
- **State**: open → active → blocked/completed/cancelled/deferred
- **Dependencies**: other goals or prerequisites
- **Progress**: tracked via task steps or custom metrics
- **Evidence**: what supports this goal's importance
- **Next action**: deterministic next step
- **Success criteria**: how Sali knows the goal is achieved
- **Cancellation conditions**: when to abandon the goal

### Extensions needed:
1. **Goal from working memory**: `active_problem` entries can generate goals
2. **Goal from curiosities**: high-priority curiosities become learning goals
3. **Goal from failures**: repeated failures generate maintenance goals
4. **Goal lifecycle management**: complete, abandon, defer, reprioritize

### Files to modify:
- `src/sali/tools/builtins/cognitive_tool.py` — `create_goal` already exists
- `src/sali/runtime/initiative.py` — add goal generation from working memory and curiosities
- `src/sali/tasks/goals.py` — extend with lifecycle methods

---

## 10. Hypothesis System

### Current State

- `form_hypothesis` tool: persists to working memory
- `run_experiment` tool: executes commands and records outcomes
- No persistent hypothesis tracking beyond working memory

### Target Architecture

Hypotheses have:
- **Observation**: what triggered the hypothesis
- **Explanation**: proposed cause
- **Prediction**: expected observable outcome
- **Confidence**: 0.0-1.0
- **Evidence**: collected observations
- **Status**: formed → testing → confirmed/rejected/needs_more_evidence

### Extensions needed:
1. **Hypothesis table**: durable storage beyond working memory
2. **Evidence linking**: connect observations to hypotheses
3. **Automatic verification**: when a tool result matches a prediction, update hypothesis confidence
4. **Contradiction handling**: when evidence contradicts a hypothesis, flag it

### New files:
- `src/sali/runtime/hypothesis.py` — HypothesisStore with lifecycle management
- Migration for `sali.hypothesis` table

---

## 11. Learning System

### Current State

- `LearningService.consolidate()`: background consolidation (procedures, tool experience, episodes, reflection)
- `ExperienceStore`: extracts experience from completed tasks
- `CuriosityStore`: tracks knowledge gaps
- `BehaviorStore`: user feedback → behavior proposals
- All passive — LLM cannot initiate learning

### Target Architecture

Learning is **active**: Sali decides what to learn based on:
- Failures (what went wrong)
- Successes (what worked well)
- Contradictions (what conflicts)
- Knowledge gaps (what's unknown)
- User feedback (what Almir wants)
- Pattern recognition (what repeats)

### Extensions needed:
1. **Lesson learning tool**: `learn_lesson` already exists — wire it to the procedural memory
2. **Experience capture from non-task interactions**: already implemented in loop.py
3. **Strategy learning**: track which reasoning approaches work (Phase 12 below)
4. **Failure analysis**: when a tool fails, automatically investigate why

### Files to modify:
- `src/sali/learning/service.py` — add `learn_from_failure()` method
- `src/sali/runtime/loop.py` — experience capture already implemented

---

## 12. Strategy System

### Current State

No strategy tracking exists. Sali has procedures (how to do X) but not strategies (how to approach problems).

### Target Architecture

Strategies are **reusable reasoning approaches** that apply across many tasks:

```python
@dataclass
class Strategy:
    name: str                    # "diagnose_before_modifying"
    description: str             # "Always inspect the current state before making changes"
    context: str                 # "When a system is behaving unexpectedly"
    evidence_count: int          # how many times this strategy was used
    success_count: int           # how many times it led to success
    confidence: float            # derived from success/evidence ratio
    contraindications: list[str] # when NOT to use this strategy
```

### New files:
- `src/sali/learning/strategy.py` — StrategyStore with evidence tracking
- Migration for `sali.strategy` table

### Integration:
- When a task succeeds, extract the reasoning approach as a strategy
- When a strategy is relevant to a new task, inject it into context
- Track strategy effectiveness over time

---

## 13. Reflection System

### Current State

- `reflect_on` cognitive tool exists (persists to memory)
- Self-reflective post-generation pass catches fabrications
- `LearningService.reflect_on_recent()` runs in background consolidation
- No structured reflection on specific decisions or outcomes

### Target Architecture

Reflection is **triggered by events**, not just scheduled:
- After a task completes (or fails)
- After a prediction is verified (or contradicted)
- After a strategy succeeds (or fails)
- After a significant conversation
- When the executive detects a pattern

### Extensions needed:
1. **Decision reflection**: after a significant decision, analyze whether it was correct
2. **Strategy reflection**: after using a strategy, evaluate its effectiveness
3. **Failure reflection**: after a failure, extract the root cause and lesson

### Files to modify:
- `src/sali/runtime/loop.py` — add reflection triggers after task completion and failure
- `src/sali/tools/builtins/cognitive_tool.py` — `reflect_on` already exists

---

## 14. Continuous Life Loop

### Current State

Background faculties run on timers:
- Twin refresh: every 300s
- Task continuation: every 45s
- Grounding: every 300s
- Initiative: every 120s
- Consolidation: hourly
- VRAM janitor: every 600s

These are **independent scheduled jobs**, not a coherent cognitive loop.

### Target Architecture

A unified **life loop** that coordinates cognition:

```
PERCEIVE (twin refresh, syswatch, perception)
→ UPDATE WORLD MODEL (graph updates)
→ UPDATE SELF STATE (sali_state)
→ UPDATE ATTENTION (rank candidates)
→ EVALUATE GOALS (check progress, deadlines)
→ SELECT PRIORITY (highest attention candidate)
→ DETERMINE WHETHER COGNITION IS NEEDED (cheap filter)
→ THINK (LLM call via executive)
→ RETRIEVE (active memory search)
→ HYPOTHESIZE (if uncertainty detected)
→ PLAN (if action needed)
→ ACT (tool execution)
→ OBSERVE (tool results)
→ VERIFY (reality check)
→ LEARN (extract lessons)
→ REFLECT (analyze outcomes)
→ UPDATE MEMORY (persist what was learned)
→ UPDATE STRATEGIES (if strategy evidence changed)
→ RE-EVALUATE GOALS (did this advance any goal?)
→ REST / WAIT (idle until next attention signal)
→ PERCEIVE AGAIN
```

### Implementation

This is NOT a single infinite loop. It's a **coordinated cycle** that:
1. Uses the existing background faculties for perception
2. Uses the cognitive executive for decision-making
3. Uses the existing coordinator for single-cognition serialization
4. Uses the existing lease for cross-process coordination
5. Persists state between cycles via working memory + PostgreSQL

### Key design: cheap perception, expensive cognition

- **Perception** (twin refresh, syswatch): runs continuously, deterministic, cheap
- **Attention** (ranking candidates): deterministic, cheap
- **Cognition** (LLM reasoning): expensive, bounded, intentional
- **Learning** (consolidation, reflection): moderate, scheduled

The executive decides WHEN cognition is needed. Not every perception requires thought.

### Files to modify:
- `src/sali/cli/main.py` — restructure background faculties into coordinated life loop
- NEW: `src/sali/runtime/life_loop.py` — the coordination layer
- `src/sali/cognitive/initiative_driver.py` — feed into attention system

---

## 15. Body

### Current State

74 tools registered. ToolContext provides sinks for memory, graph, tasks, research, browser, vision, etc. PathGuard enforces filesystem boundaries. Policy engine gates risk.

### Target Architecture

The body is **everything Sali can do that isn't thinking**:
- Execute shell commands
- Read/write files
- Browse the web
- Manage tasks
- Send messages
- Control the browser
- Use vision
- Manage memory
- Query the graph

The body is the **enforcement layer**. The cognitive executive decides what to do; the body determines what is possible and enforces safety.

### No changes needed to the body architecture.
The existing tool system, PathGuard, policy engine, and verification pipeline are well-designed. They stay as-is.

### Files to reuse unchanged:
- `src/sali/tools/` — all tool implementations
- `src/sali/tools/pathguard.py` — filesystem boundaries
- `src/sali/security/policy.py` — risk assessment
- `src/sali/verify/` — verification engine

---

## 16. Safety Boundary

### Cognitive Freedom vs Operational Authority

| Cognitive Freedom (Sali decides) | Operational Authority (system enforces) |
|----------------------------------|----------------------------------------|
| What to think about | Whether an action is permitted |
| What to investigate | Risk assessment |
| What to learn | Authorization (owner/controller/observer) |
| What to remember | Protected paths |
| What goals to create | Irreversible action safeguards |
| What hypotheses to form | Consent requirements |
| What strategies to try | Resource limits |
| When to communicate with Almir | Verification of effects |

### Rules:
1. Cognitive freedom is NEVER restricted — Sali can think about anything
2. Operational authority is ALWAYS enforced — the body checks every action
3. The executive can REQUEST an action; the body DECIDES whether to permit it
4. Safety is in the body, not in the prompt
5. The LLM can be tricked; the policy engine cannot

### Files to reuse unchanged:
- `src/sali/security/policy.py`
- `src/sali/runtime/judgment.py`
- `src/sali/runtime/authority.py`
- `src/sali/runtime/envelope.py`
- `src/sali/tools/pathguard.py`

---

## 17. API Boundary

### What the iOS app sees (unchanged):

| Interface | Contract |
|-----------|----------|
| `POST /api/v1/conversation/message` | Send message, get 202 accepted |
| `GET /api/v1/conversation` | Get conversation history |
| `WS /ws` | Real-time events, recovery protocol |
| `GET /api/v1/tasks` | List tasks |
| `GET /api/v1/health` | System health |
| All other endpoints | Unchanged response shapes |

### What happens behind the API (can change):

| Component | Current | Target |
|-----------|---------|--------|
| Message handling | FSM-driven | Executive-driven |
| Context assembly | Deterministic router | Executive + deterministic router |
| Memory retrieval | Pre-assembled | Pre-assembled + active LLM queries |
| Tool execution | FSM dispatches | Executive decides, body executes |
| Background work | Independent timers | Coordinated life loop |
| Learning | Background consolidation | Active learning from every interaction |

### New events (additive, not breaking):
- `cognitive.state_changed` — Sali's cognitive state updated
- `cognitive.goal_created` — Sali created a new goal
- `cognitive.insight` — Sali learned something new
- `cognitive.attention` — Sali's attention shifted

### New endpoints (additive):
- `GET /api/v1/cognitive/executive` — executive state snapshot
- `GET /api/v1/cognitive/attention` — current attention candidates
- `GET /api/v1/cognitive/strategies` — learned strategies

### Files to modify:
- `src/sali/api/routes/api.py` — add new endpoints
- `src/sali/api/ws.py` — add new event types

---

## Implementation Map

### Files to CREATE:

| File | Purpose |
|------|---------|
| `src/sali/runtime/executive.py` | CognitiveExecutive class |
| `src/sali/runtime/life_loop.py` | Coordinated life loop |
| `src/sali/runtime/hypothesis.py` | HypothesisStore |
| `src/sali/learning/strategy.py` | StrategyStore |
| `src/sali/db/migrations/0066_working_memory.sql` | Working memory table |
| `src/sali/db/migrations/0067_hypothesis.sql` | Hypothesis table |
| `src/sali/db/migrations/0068_strategy.sql` | Strategy table |
| `tests/test_agi_behavior.py` | AGI behavioral tests |

### Files to MODIFY:

| File | Changes |
|------|---------|
| `src/sali/runtime/loop.py` | Extract cognitive decisions into executive, add active memory queries |
| `src/sali/runtime/attention.py` | Add AttentionCandidate, scoring, attention sources |
| `src/sali/runtime/cognitive.py` | Add performance metrics, strategy state |
| `src/sali/context/engine.py` | Accept executive-provided context hints |
| `src/sali/cognitive/initiative_driver.py` | Feed into attention system |
| `src/sali/cli/main.py` | Coordinate background faculties into life loop |
| `src/sali/tools/builtins/cognitive_tool.py` | Already created — extend with new tools |
| `src/sali/graph/service.py` | Add temporal queries |

### Files UNCHANGED:

| File | Why |
|------|-----|
| `src/sali/api/*` | API contract preserved |
| `src/sali/tools/` | Body/tools unchanged |
| `src/sali/security/` | Safety unchanged |
| `src/sali/verify/` | Verification unchanged |
| `src/sali/memory/writer.py` | Write path unchanged |
| `src/sali/memory/retriever.py` | Retrieval unchanged |
| `src/sali/tasks/store.py` | Task lifecycle unchanged |
| `src/sali/db/migrations/0001-0065` | Existing migrations unchanged |

### Migration Strategy:

1. Each phase adds new tables/modules — never modifies existing schemas destructively
2. New columns added with defaults — existing code continues to work
3. New tools added to registry — existing tools unchanged
4. New context sections added — existing sections unchanged
5. New API endpoints added — existing endpoints unchanged

### Rollback Strategy:

Each phase is independently reversible:
- New tables can be dropped without affecting existing functionality
- New tools can be unregistered without affecting existing tools
- New context sections can be removed without affecting existing context
- New API endpoints can be removed without affecting existing endpoints

---

## Phase 1 — Cognitive Executive (implement first)

**Goal:** The LLM decides what to think about, not the FSM.

**Changes:**
1. Create `src/sali/runtime/executive.py` with `CognitiveExecutive`
2. Modify `src/sali/runtime/loop.py` to call executive before LLM
3. Add `src/sali/runtime/attention.py` with `AttentionCandidate`
4. Wire working memory into attention
5. Add tests demonstrating that the executive makes decisions the FSM didn't

**Success criteria:**
- Sali can decide "I need to query my memory about X" without the router deciding
- Sali can decide "I should reflect on what just happened" without being prompted
- Sali can decide "This deserves investigation" and create a curiosity
- All existing tests pass
- API/WebSocket unchanged

### Phase 2 — Persistent Working Memory (already partially done)

### Phase 3 — Hypothesis Engine

### Phase 4 — Goal Architecture

### Phase 5 — Strategy Learning

### Phase 6 — Continuous Life Loop

### Phase 7 — Active Learning

### Phase 8 — Self-Diagnosis

### Phase 9 — Transfer Learning

### Phase 10 — Long-Running Autonomy

Each phase builds on the previous. No phase requires a rewrite. Every phase is testable, observable, and reversible.
