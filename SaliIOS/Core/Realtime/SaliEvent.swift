import Foundation

/// A real-time event from Sali's durable event log, as delivered over the WebSocket (live or replayed) or
/// read back from `GET /api/v1/events`. The shape is identical for all of them so the UI handles them
/// uniformly (see docs/API_REFERENCE.md §6). `data` is kept as a loosely-typed bag because each event type
/// carries its own fields — the app reads only the high-level, operational keys it needs and NEVER any
/// chain-of-thought.
public struct SaliEvent: Identifiable, Sendable, Equatable {
    public let id: String
    public let type: EventType
    public let rawType: String
    public let sequence: Int?
    public let timestamp: Date?
    public let taskId: String?
    public let runId: String?
    /// Which conversation this belongs to. Sali's OWN task-continuation turns run in a
    /// separate work session, so chat must ignore turn events that are not its own —
    /// otherwise task chatter streams into the transcript live and then vanishes on
    /// reload, because GET /conversation is scoped to the persistent chat session.
    public let sessionId: String?
    public let origin: String?
    public let data: [String: JSONValue]
    public let replayed: Bool

    /// The high-level, human-facing description for the Activity center (never internal reasoning).
    public var humanSummary: String {
        switch type {
        case .messageStarted:   return "Sali is responding…"
        case .messageDelta:     return "Sali is responding…"
        case .messageCompleted: return "Sali finished responding"
        // Not shown as an activity label - the handler seals what was streamed and moves on.
        case .messageIterationBoundary: return "…"
        case .toolStarted:      return "Working: \(string("tool") ?? string("name") ?? "a tool")"
        case .toolProgress:     return string("text")?.capitalized ?? string("summary") ?? "Thinking…"
        case .toolCompleted:    return "Finished: \(string("tool") ?? string("name") ?? "a step")"
        case .taskStarted:      return "Started: \(string("objective") ?? "a task")"
        case .taskProgress:
            // task.progress + task.step.started/completed/failed all collapse to .taskProgress. Read
            // the payload so a completed — or, critically, a FAILED — step is never shown as progress.
            let n = int("seq") ?? int("step") ?? int("step_seq")
            let lbl = n.map { "step \($0)" } ?? "a step"
            if string("error") != nil { return "Failed \(lbl)" }        // task.step.failed
            if bool("verified") != nil { return "Finished \(lbl)" }     // task.step.completed
            if string("description") != nil { return "Working \(lbl)" } // task.step.started
            return "Working \(lbl)"                                     // task.progress
        case .taskWaiting:      return "Waiting for your clarification"
        // `task.finished` is emitted for EVERY terminal status — done, failed, abandoned (store.py:457
        // passes the status straight through). Rendering them all as "Task complete" told the user a
        // failed task had succeeded, which is the one thing an inbox must never do.
        case .taskCompleted:    return Self.finishedSummary(string("status"))
        case .taskSuspended:    return "Paused"
        case .taskResumed:      return "Resumed"
        case .researchStarted:  return "Searching documentation…"
        case .researchCompleted:return "Research complete"
        case .researchReportReady: return "Report ready to download"
        case .fileSent:         return "Sali sent you a file"
        case .agentMessage:     return string("text") ?? "Sali has a message for you"
        // The payload is {incident_id, kind, severity, workload} (incidents.py:31). `kind` is a machine
        // identifier — it reached the screen as "Resource incident: resource_pressure", which shows a
        // snake_case symbol AND says "resource" twice. `workload` names what was running when the host
        // came under pressure, which is the part you can act on, and it was thrown away.
        case .resourceIncident:
            let what = Self.humanized(string("kind")) ?? "Host pressure"
            if let load = text("workload") { return "Incident: \(what) — \(load)" }
            return "Incident: \(what)"
        case .resourceState:    return "System state: \(string("state") ?? "changed")"
        // {task_id, reason, objective} (revocation.py:56) — both the objective and the reason were
        // dropped, leaving a row that said a task was abandoned without saying which one, or why.
        case .intentRevoked:
            if let objective = text("objective") { return "Abandoned: \(objective)" }
            if let reason = text("reason") { return "A task was abandoned — \(reason)" }
            return "A task was abandoned"
        case .error:            return string("error") ?? "Something went wrong"
        case .connected, .subscribed, .pong: return rawType
        case .other(let family): return family.summary(for: self)
        }
    }

    /// A terminal task status as a sentence. Unknown statuses are surfaced verbatim rather than being
    /// flattened into "complete" — a status this app has not been taught is still a fact about the task.
    private static func finishedSummary(_ status: String?) -> String {
        switch status?.lowercased() {
        case "done", nil:   return "Task complete"
        case "failed":      return "Task failed"
        case "abandoned":   return "Task abandoned"
        case "cancelled":   return "Task cancelled"
        case "superseded":  return "Task superseded"
        case let other?:    return "Task \(other.replacingOccurrences(of: "_", with: " "))"
        }
    }

    /// A snake_case machine identifier as a human phrase. Returns nil for nil/empty so callers keep their
    /// own fallback wording rather than printing an empty label.
    private static func humanized(_ raw: String?) -> String? {
        guard let raw, !raw.isEmpty else { return nil }
        let spaced = raw.replacingOccurrences(of: "_", with: " ")
        return spaced.prefix(1).uppercased() + spaced.dropFirst()
    }

    public func string(_ key: String) -> String? {
        if case let .string(s)? = data[key] { return s }
        if case let .number(n)? = data[key] { return String(n) }
        if case let .bool(b)? = data[key] { return String(b) }
        return nil
    }
    public func bool(_ key: String) -> Bool? {
        if case let .bool(b)? = data[key] { return b }
        return nil
    }
    public func int(_ key: String) -> Int? {
        if case let .number(n)? = data[key] { return Int(n) }
        if case let .string(s)? = data[key] { return Int(s) }
        return nil
    }

    /// A payload string that is safe to put in a sentence: trimmed, single-line, and empty treated as
    /// missing. Payload text is already capped server-side (120–200 chars at the emit sites).
    func text(_ key: String) -> String? {
        guard let raw = string(key) else { return nil }
        let cleaned = raw.replacingOccurrences(of: "\n", with: " ")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return cleaned.isEmpty ? nil : cleaned
    }
}

public enum EventType: Sendable, Equatable, Hashable {
    case messageStarted, messageDelta, messageCompleted
    /// Emitted by the backend right before it dispatches tool calls for an iteration whose text was
    /// NOT the final answer. The app takes what has been streamed for the current iteration, moves
    /// it into the activity trail as a "said" step, and resets the streaming bubble for the next
    /// iteration. Only the LAST iteration's tokens end up in the chat bubble, so "let me check /
    /// now I'll / first I need to" stays visible as progress narration where it belongs and does
    /// not clutter the intentional message to Almir.
    case messageIterationBoundary
    case toolStarted, toolProgress, toolCompleted
    case taskStarted, taskProgress, taskWaiting, taskCompleted, taskSuspended, taskResumed
    case researchStarted, researchCompleted
    /// A research turn saved a full write-up as a downloadable file (backend: research.report_ready).
    /// The chat shows the summary; this attaches the report file as a small chip to download/save.
    case researchReportReady
    /// Sali sent Almir a file via the send_file tool (backend: file.sent) — a report, a build output,
    /// a zipped project. Attaches it to the reply as a downloadable file (Almir is on his phone, so a
    /// local path can't reach him; this is how he actually gets a file).
    case fileSent
    case agentMessage, resourceIncident, resourceState, intentRevoked, error
    case connected, subscribed, pong
    /// Everything the chat turn-stream doesn't own. It used to be a bare catch-all, which is why ~40 of the
    /// ~60 event types the backend really emits landed here and were then filtered out of every surface —
    /// Sali's whole autonomous life was invisible. It now carries the classified family (see `EventFamily`),
    /// so the Activity center can render it while the turn-stream consumers keep treating `.other` as
    /// "not part of this reply".
    case other(EventFamily)

    // Mapped to the REAL event names the backend emits (verified against src/sali/runtime/coordinator.py
    // `agent.{kind}` and src/sali/tasks/store.py `task.*`). The idealized message.*/task.completed names
    // are never produced (Final audit §29/§31), so an app keyed to them would render nothing.
    init(rawType: String) {
        switch rawType {
        // Turn stream — coordinator publishes agent.{run,status,thinking,retrieval,token,tool,final}.
        case "agent.run":        self = .messageStarted
        case "agent.token":      self = .messageDelta      // a streamed chunk; text in data.text
        case "agent.final":      self = .messageCompleted  // the settled assistant reply
        case "agent.thinking", "agent.status", "agent.retrieval": self = .toolProgress
        // Iter N's content was progress narration - the model returned with tool calls, so this was
        // not the final answer. See EventType.messageIterationBoundary above for the whole rationale.
        case "agent.iteration_boundary": self = .messageIterationBoundary
        case "agent.tool", "task.tool.started": self = .toolStarted
        case "tool.completed", "task.tool.completed": self = .toolCompleted
        // Task lifecycle — store.py emits task.* (note: 'finished' with data.status, not 'completed').
        case "task.created", "task.activated": self = .taskStarted
        // task.step.started (backend: driver picked the step, pending→running) refreshes the view so
        // the ONE ongoing step shows live, same as progress/completed/failed.
        case "task.progress", "task.step.started", "task.step.completed", "task.step.failed":
            self = .taskProgress
        case "task.waiting_for_user": self = .taskWaiting
        case "task.finished":    self = .taskCompleted
        case "task.suspended":   self = .taskSuspended
        case "task.resumed":     self = .taskResumed
        case "research.started": self = .researchStarted
        case "research.completed": self = .researchCompleted
        case "research.report_ready": self = .researchReportReady
        case "file.sent":        self = .fileSent
        // Proactive + lifecycle signals.
        case "agent.message":    self = .agentMessage      // Sali reaching out (send_agent_message)
        case "resource.incident_recorded": self = .resourceIncident
        case "intent.revoked":   self = .intentRevoked
        case "error", "agent.error": self = .error
        case "connected":        self = .connected
        case "subscribed":       self = .subscribed
        case "pong":             self = .pong
        default:                 self = .other(EventFamily(rawType: rawType))
        }
    }

    /// True for a frame the app has no meaning for — connection housekeeping, or a real backend event with
    /// no screen behind it yet. Feeds filter on this instead of hiding everything that isn't chat.
    public var isBackgroundNoise: Bool {
        switch self {
        case .connected, .subscribed, .pong: return true
        case .other(let family): return family == .unknown
        default: return false
        }
    }
}

/// The families of Sali's autonomous life that already have a screen in this app — Life (goals, initiatives,
/// obligations, commitments, activities, real-world actions), Learning (lessons, behaviour proposals,
/// capabilities), Memory (experiences, twin), Schedules (routines), System (runtime/context/desktop), Tasks
/// and Settings (consent).
///
/// EVERY case here maps to an event name the backend genuinely emits — verified read-only against
/// `src/sali/**` emit sites (goals.py, initiatives.py, commitments.py, activity.py, digital.py,
/// side_effects.py, routines.py, candidates.py, behavior.py, experience.py, capability.py, consent.py,
/// twin/sync.py, ledger.py, store.py, watchdog.py, runtime/loop.py, runtime/runtime.py). Nothing is invented:
/// an unrecognised name stays `.unknown` and is shown only in technical detail, never dressed up as
/// something it isn't.
public enum EventFamily: Sendable, Equatable, Hashable {
    // Life — goals that outlive a single task
    case goalCreated, goalUpdated, goalCompleted, goalBlocked
    // Life — initiatives Sali chose for itself
    case initiativeCreated, initiativeSelected, initiativeCompleted, initiativeBlocked, initiativeDeferred
    // Life — promises: obligations and commitments
    case obligationCreated, obligationResolved, obligationReopened
    case commitmentCreated, commitmentFulfilled, commitmentCancelled, commitmentOverdue
    // Life — what Sali is doing right now
    case activityStarted, activityProgressed, activityCompleted
    case activityFailed, activityBlocked, activityResumed
    // Life — acting on the world outside itself
    case digitalActionStarted, digitalActionProgressed, digitalActionVerified
    case digitalActionWaiting, digitalActionBlocked, digitalActionFailed
    case sideEffectPlanned, sideEffectSucceeded, sideEffectFailed, sideEffectRecovered
    case intentRevocationPropagated
    // Schedules — recurring work on Sali's own clock
    case routineCompleted, routineFailed
    // Learning — lessons
    case learningObserved, learningCandidate, learningVerified, learningPromoted, learningRejected
    case learningContradicted, learningSuperseded
    case learningReflection, learningProcedure, learningToolExperience, learningFromFailure
    case learningDailyStarted, learningDailyCompleted, learningDailyFailed
    // Learning — proposals to change how Sali works (you are the authority, §42)
    case behaviorProposed, behaviorTesting, behaviorAccepted, behaviorRejected, behaviorSuperseded
    // Learning / System — capabilities
    case capabilityDiscovered, capabilityVerified, capabilityAttempted, capabilityUsed
    case capabilityDegraded, capabilityFailed
    // Memory
    case experienceRecorded, experienceVerified, experienceLesson
    case memoryConsolidated, memoryRetrieved, knowledgeRetrieved
    case twinSynced, twinEntityAdded, twinEntityRemoved
    // Settings — consent
    case consentRequested, consentGranted, consentModified, consentRevoked, consentExpired
    // Chat / Activity — attention: what happens to Sali's work when you interrupt it
    case attentionInterrupted, attentionCancelled, attentionResumed
    // System — runtime, context pressure, the desktop
    case contextLimitApproaching, contextCompactionStarted, contextCompactionCompleted
    case contextCompactionFailed, contextOverflowRecovered, contextEmergencyMode
    case runtimeRecoveryStarted, runtimeRecoveryCompleted, continuationStarted, continuationCompleted
    case desktopObserved
    // Tasks — the lifecycle detail beyond created/finished
    case taskPhaseStarted, taskPhaseCompleted, taskDecision, taskArtifact
    case taskHealthChanged, taskRepeatedFailure, taskCompletionRejected, taskUserAnswered
    case taskCancelled, taskSuperseded
    /// Turn 6: mid-task correction landed via modify_task (add/remove/replace/reset/edit_step/change_objective).
    /// Payload carries `action` and the target `step` or `objective`.
    case taskModified
    // Tools and research that didn't go to plan
    case toolFailed, toolDenied, toolUnavailable
    case researchFailed, researchUsed
    // Proactive / run lifecycle
    case reengagement, runInterrupted, runError
    // Persistent-organism autonomy surface (backend emits from OpenLoopStore, CuriosityStore,
    // CommunicationDecisionEngine, and coordinator.background_wedged). Without these cases they
    // all fall through to .unknown and get filtered out of the Activity/Tasks views as noise.
    case openLoopCreated, openLoopReinforced, openLoopResolved, openLoopDismissed
    case curiosityCreated, curiosityReinforced, curiosityLearned, curiosityResolved
    case proactiveMessageSent, proactiveMessageSuppressed
    case initiativeDriverCycle
    case runtimeBackgroundWedged
    case skillProposalCreated, skillProposalAccepted, skillProposalRejected
    /// A real event with no screen behind it yet, or one this build doesn't recognise. Never guessed at.
    case unknown

    // MARK: - The real backend names

    init(rawType: String) {
        switch rawType {
        // ── Life: goals (src/sali/tasks/goals.py) ─────────────────────────────────────────────────────
        case "goal.created":                    self = .goalCreated
        case "goal.updated":                    self = .goalUpdated
        case "goal.completed":                  self = .goalCompleted
        case "goal.blocked":                    self = .goalBlocked
        // ── Life: initiatives (src/sali/tasks/initiatives.py) ─────────────────────────────────────────
        case "life.initiative.created":         self = .initiativeCreated
        case "life.initiative.selected":        self = .initiativeSelected
        case "life.initiative.completed":       self = .initiativeCompleted
        case "life.initiative.blocked":         self = .initiativeBlocked
        case "life.initiative.deferred":        self = .initiativeDeferred
        // ── Life: obligations & commitments (src/sali/tasks/commitments.py) ───────────────────────────
        case "obligation.created":              self = .obligationCreated
        case "obligation.resolved":             self = .obligationResolved
        case "obligation.reopened":             self = .obligationReopened
        case "commitment.created":              self = .commitmentCreated
        case "commitment.fulfilled":            self = .commitmentFulfilled
        case "commitment.cancelled":            self = .commitmentCancelled
        case "commitment.overdue":              self = .commitmentOverdue
        // ── Life: the current activity (src/sali/tasks/activity.py) ───────────────────────────────────
        case "activity.started":                self = .activityStarted
        case "activity.progressed":             self = .activityProgressed
        case "activity.completed":              self = .activityCompleted
        case "activity.failed":                 self = .activityFailed
        case "activity.blocked":                self = .activityBlocked
        case "activity.resumed":                self = .activityResumed
        // ── Life: acting on the world (src/sali/tasks/digital.py, side_effects.py) ────────────────────
        case "digital_action.started":          self = .digitalActionStarted
        case "digital_action.progressed":       self = .digitalActionProgressed
        case "digital_action.verified":         self = .digitalActionVerified
        case "digital_action.awaiting_external_state": self = .digitalActionWaiting
        case "digital_action.blocked":          self = .digitalActionBlocked
        case "digital_action.failed":           self = .digitalActionFailed
        case "side_effect.planned":             self = .sideEffectPlanned
        case "side_effect.succeeded":           self = .sideEffectSucceeded
        case "side_effect.failed":              self = .sideEffectFailed
        case "side_effect.recovered":           self = .sideEffectRecovered
        case "intent.revocation_propagated":    self = .intentRevocationPropagated
        // ── Schedules: routines (src/sali/tasks/routines.py) ──────────────────────────────────────────
        case "routine.completed":               self = .routineCompleted
        case "routine.failed":                  self = .routineFailed
        // ── Learning: lessons (src/sali/learning/candidates.py, service.py, daily.py) ─────────────────
        case "learning.observed":               self = .learningObserved
        case "learning.candidate_created", "learning.candidate_updated", "learning.queued":
                                                self = .learningCandidate
        case "learning.verified":               self = .learningVerified
        case "learning.promoted":               self = .learningPromoted
        case "learning.rejected":               self = .learningRejected
        case "learning.contradicted":           self = .learningContradicted
        case "learning.superseded":             self = .learningSuperseded
        case "learning.reflection", "learning.episode": self = .learningReflection
        case "learning.procedure", "learning.procedure_outcome": self = .learningProcedure
        case "learning.tool_experience":        self = .learningToolExperience
        case "learning.failure":                self = .learningFromFailure
        case "learning.daily_started":          self = .learningDailyStarted
        case "learning.daily_completed":        self = .learningDailyCompleted
        case "learning.daily_failed":           self = .learningDailyFailed
        // ── Learning: behaviour proposals (src/sali/learning/behavior.py) ─────────────────────────────
        case "behavior.candidate_created":      self = .behaviorProposed
        case "behavior.testing":                self = .behaviorTesting
        case "behavior.accepted":               self = .behaviorAccepted
        case "behavior.rejected":               self = .behaviorRejected
        case "behavior.superseded":             self = .behaviorSuperseded
        // ── Learning/System: capabilities (src/sali/learning/capability*.py) ──────────────────────────
        case "capability.discovered":           self = .capabilityDiscovered
        case "capability.verified":             self = .capabilityVerified
        case "capability.attempted":            self = .capabilityAttempted
        case "capability.used":                 self = .capabilityUsed
        case "capability.degraded":             self = .capabilityDegraded
        case "capability.failed":               self = .capabilityFailed
        // ── Memory (src/sali/learning/experience.py, retrieval.py, src/sali/twin/sync.py) ─────────────
        case "experience.recorded":             self = .experienceRecorded
        case "experience.verified":             self = .experienceVerified
        case "experience.lesson_extracted":     self = .experienceLesson
        case "memory.consolidated":             self = .memoryConsolidated
        case "memory.retrieved":                self = .memoryRetrieved
        case "knowledge.retrieved":             self = .knowledgeRetrieved
        case "twin.synced":                     self = .twinSynced
        case "twin.entity_added":               self = .twinEntityAdded
        case "twin.entity_removed":             self = .twinEntityRemoved
        // ── Settings: consent (src/sali/tasks/consent.py) ─────────────────────────────────────────────
        case "consent.requested":               self = .consentRequested
        case "consent.granted":                 self = .consentGranted
        case "consent.modified":                self = .consentModified
        case "consent.revoked":                 self = .consentRevoked
        case "consent.expired":                 self = .consentExpired
        // ── Attention (src/sali/runtime/runtime.py). The per-turn bookkeeping
        //    (turn_started/turn_completed, message_received/classified, interrupt_run_*) is plumbing and
        //    stays `.unknown`; only what a person would notice is surfaced.
        case "attention.primary_interrupted":   self = .attentionInterrupted
        case "attention.primary_cancelled":     self = .attentionCancelled
        case "attention.resume_run_started":    self = .attentionResumed
        // ── System: runtime & context pressure (src/sali/runtime/loop.py) ─────────────────────────────
        case "runtime.context_approaching_limit":    self = .contextLimitApproaching
        case "runtime.context_compaction_started":   self = .contextCompactionStarted
        case "runtime.context_compaction_completed": self = .contextCompactionCompleted
        case "runtime.context_compaction_failed":    self = .contextCompactionFailed
        case "runtime.context_overflow_recovered":   self = .contextOverflowRecovered
        case "context.emergency_mode":               self = .contextEmergencyMode
        case "runtime.recovery_started":             self = .runtimeRecoveryStarted
        case "runtime.recovery_completed":           self = .runtimeRecoveryCompleted
        case "runtime.continuation_started":         self = .continuationStarted
        case "runtime.continuation_completed":       self = .continuationCompleted
        case "desktop.observed":                     self = .desktopObserved
        // ── Tasks: the detail beyond created/finished (ledger.py, store.py, watchdog.py) ──────────────
        //    `task.step_advanced` is deliberately absent: store.py emits `task.progress` alongside it, and
        //    two rows for one step would be noise, not information.
        case "task.phase_started":              self = .taskPhaseStarted
        case "task.phase_completed":            self = .taskPhaseCompleted
        case "task.decision_created":           self = .taskDecision
        case "task.artifact.created":           self = .taskArtifact
        case "task.health_changed":             self = .taskHealthChanged
        case "task.repeated_failure_detected":  self = .taskRepeatedFailure
        case "task.completion_rejected":        self = .taskCompletionRejected
        case "task.user_answered":              self = .taskUserAnswered
        case "task.cancelled":                  self = .taskCancelled
        case "task.superseded":                 self = .taskSuperseded
        case "task.modified":                   self = .taskModified   // Turn 6: mid-task correction
        // ── Tools & research that didn't go to plan (runtime/loop.py, tasks/research.py) ──────────────
        case "tool.failed", "task.tool.failed": self = .toolFailed
        case "tool.denied":                     self = .toolDenied
        case "tool.unknown":                    self = .toolUnavailable
        case "research.failed":                 self = .researchFailed
        case "research.used":                   self = .researchUsed
        // ── Proactive / run lifecycle (tasks/conversation.py, runtime/loop.py) ────────────────────────
        case "agent.reengagement":              self = .reengagement
        case "run.interrupted":                 self = .runInterrupted
        case "run.error":                       self = .runError
        // ── Persistent-organism autonomy (open loops, curiosities, proactive gate, driver, skills) ──
        case "open_loop.created":               self = .openLoopCreated
        case "open_loop.reinforced":            self = .openLoopReinforced
        case "open_loop.resolved":              self = .openLoopResolved
        case "open_loop.dismissed":             self = .openLoopDismissed
        case "curiosity.created", "curiosity.encountered":  self = .curiosityCreated
        case "curiosity.reinforced":            self = .curiosityReinforced
        case "curiosity.learned":               self = .curiosityLearned
        case "curiosity.resolved":              self = .curiosityResolved
        case "proactive.message.sent":          self = .proactiveMessageSent
        case "proactive.message.suppressed":    self = .proactiveMessageSuppressed
        case "initiative_driver.cycle":         self = .initiativeDriverCycle
        case "runtime.background_wedged":       self = .runtimeBackgroundWedged
        case "skill.proposal_created":          self = .skillProposalCreated
        case "skill.proposal_accepted":         self = .skillProposalAccepted
        case "skill.proposal_rejected":         self = .skillProposalRejected
        default:                                self = .unknown
        }
    }

    // MARK: - Calm, human sentences (high-level only: never reasoning, never a payload dump)

    func summary(for event: SaliEvent) -> String {
        switch self {
        // Life — goals
        case .goalCreated:      return "New goal: \(event.text("objective") ?? "something to work towards")"
        case .goalUpdated:      return "Made progress on a goal"
        case .goalCompleted:    return "Reached a goal"
        case .goalBlocked:      return "Goal blocked: \(event.text("reason") ?? "something is in the way")"
        // Life — initiatives
        case .initiativeCreated:   return "Thought of something to do: \(event.text("title") ?? "a new initiative")"
        case .initiativeSelected:  return "Chose to work on: \(event.text("title") ?? "an initiative")"
        case .initiativeCompleted: return "Finished something it chose to do"
        case .initiativeBlocked:   return "An initiative is blocked"
        case .initiativeDeferred:  return "Set an initiative aside for later"
        // Life — promises
        case .obligationCreated:   return "Took something on: \(event.text("description") ?? "a new obligation")"
        case .obligationResolved:  return "Settled an obligation"
        case .obligationReopened:  return "Reopened an obligation"
        case .commitmentCreated:   return "Committed to: \(event.text("description") ?? "something for you")"
        case .commitmentFulfilled: return "Kept a commitment"
        case .commitmentCancelled: return "Cancelled a commitment"
        case .commitmentOverdue:   return "A commitment is overdue"
        // Life — current activity
        case .activityStarted:
            return "Started: \(event.text("description") ?? event.text("kind") ?? "something new")"
        case .activityProgressed:  return event.text("note") ?? "Still working"
        case .activityCompleted:   return "Finished what it was doing"
        case .activityFailed:      return "That didn't work out"
        case .activityBlocked:     return "Blocked on what it was doing"
        case .activityResumed:     return "Picked it back up"
        // Life — acting on the world
        case .digitalActionStarted:
            return "Acting on: \(event.text("intent") ?? event.text("capability") ?? "the outside world")"
        case .digitalActionProgressed: return "An action is moving along"
        case .digitalActionVerified:   return "Confirmed an action landed"
        case .digitalActionWaiting:    return "Waiting on something outside Sali"
        case .digitalActionBlocked:    return "An action is blocked"
        case .digitalActionFailed:     return "An action failed"
        case .sideEffectPlanned:   return "Planned a change: \(event.text("kind") ?? "something outside itself")"
        case .sideEffectSucceeded: return "Made a change: \(event.text("kind") ?? "outside itself")"
        case .sideEffectFailed:    return "A change failed: \(event.text("kind") ?? "outside itself")"
        case .sideEffectRecovered: return "Undid a change: \(event.text("kind") ?? "outside itself")"
        case .intentRevocationPropagated: return "Cleaned up after an abandoned task"
        // Schedules
        case .routineCompleted: return "Routine finished: \(event.text("name") ?? "scheduled work")"
        case .routineFailed:    return "Routine failed: \(event.text("name") ?? "scheduled work")"
        // Learning
        case .learningObserved:   return "Noticed something worth learning"
        case .learningCandidate:  return "New lesson under review: \(event.text("lesson") ?? "something it saw")"
        case .learningVerified:   return "A lesson held up under checking"
        case .learningPromoted:   return "Learned something for good"
        case .learningRejected:   return "Dropped a lesson that didn't hold up"
        case .learningContradicted: return "Found a contradiction in what it knows"
        case .learningSuperseded: return "Replaced an older lesson with a better one"
        case .learningReflection: return "Folded recent work into memory"
        case .learningProcedure:  return "Wrote down how to do something"
        case .learningToolExperience: return "Recorded what it learned about a tool"
        case .learningFromFailure: return "Recorded what went wrong, so it doesn't repeat"
        case .learningDailyStarted:   return "Started its daily consolidation"
        case .learningDailyCompleted: return "Finished its daily consolidation"
        case .learningDailyFailed:    return "Daily consolidation didn't finish"
        // Behaviour proposals
        case .behaviorProposed:   return "Proposed a change to how it works"
        case .behaviorTesting:    return "Trying out a proposed change"
        case .behaviorAccepted:   return "A change to how Sali works was approved"
        case .behaviorRejected:   return "A proposed change was turned down"
        case .behaviorSuperseded: return "Replaced an earlier proposal"
        // Capabilities
        case .capabilityDiscovered:
            // The host emits this for TWO different things: a capability that now exists (from
            // `observe`, carrying `name`) and a GAP it has just decided to close (from `identify_gap`,
            // carrying `capability` and `missing`). Reading both as "found something new it can do"
            // announced abilities Sali does not have — the exact fakery the capability layer exists to
            // prevent. `missing` is what tells them apart.
            if event.text("missing") != nil || event.text("name") == nil,
               let gap = event.text("capability") {
                return "Hit something it can't do yet: \(gap)"
            }
            return "Found something new it can do: \(event.text("name") ?? event.text("capability") ?? "a capability")"
        case .capabilityVerified:
            return "Confirmed it can do: \(event.text("name") ?? event.text("capability") ?? "something")"
        case .capabilityAttempted:
            // Also carries the acquisition arc's stage transitions (researching → acquiring → verifying).
            if let stage = event.text("status"), let name = event.text("capability") {
                switch stage {
                case "researching": return "Reading up on: \(name)"
                case "acquiring":   return "Practising: \(name)"
                case "verifying":   return "Checking whether it really learned: \(name)"
                default:            return "Working on: \(name)"
                }
            }
            return "Tried: \(event.text("name") ?? event.text("capability") ?? "a capability")"
        case .capabilityUsed:
            return "Used: \(event.text("name") ?? event.text("capability") ?? "a capability")"
        case .capabilityDegraded:
            return "Stopped being able to: \(event.text("name") ?? "do something it could before")"
        case .capabilityFailed:
            return "A capability failed: \(event.text("name") ?? event.text("capability") ?? "one of its own")"
        // Memory
        case .experienceRecorded: return "Recorded what happened: \(event.text("objective") ?? "an experience")"
        case .experienceVerified: return "An experience checked out"
        case .experienceLesson:   return "Took a lesson from it: \(event.text("lesson") ?? "something learned")"
        case .memoryConsolidated: return "Filed it away in memory"
        case .memoryRetrieved:
            if let count = event.int("count"), count > 0 { return "Recalled \(count) related memories" }
            return "Looked back through its memory"
        case .knowledgeRetrieved: return "Pulled up what it already knows"
        case .twinSynced:         return "Refreshed its picture of your machine"
        case .twinEntityAdded:    return "Noticed something new on your machine"
        case .twinEntityRemoved:  return "Something it knew about is gone"
        // Consent
        case .consentRequested: return "Asking permission: \(event.text("action") ?? "for something consequential")"
        case .consentGranted:   return "Permission granted: \(event.text("scope") ?? "for that kind of action")"
        case .consentModified:  return "A permission was changed"
        case .consentRevoked:   return "Permission withdrawn: \(event.text("scope") ?? "for that kind of action")"
        case .consentExpired:   return "A permission expired"
        // Attention
        case .attentionInterrupted: return "Set its work aside for your message"
        case .attentionCancelled:   return "Stopped what it was doing for your message"
        case .attentionResumed:     return "Went back to what it was doing"
        // System
        case .contextLimitApproaching:  return "Running low on room to think"
        case .contextCompactionStarted: return "Tidying its working memory"
        case .contextCompactionCompleted: return "Working memory tidied"
        case .contextCompactionFailed:  return "Couldn't tidy its working memory"
        case .contextOverflowRecovered: return "Recovered after running out of room"
        case .contextEmergencyMode:     return "Working in a reduced mode to keep going"
        case .runtimeRecoveryStarted:   return "Recovering work that was interrupted"
        case .runtimeRecoveryCompleted: return "Interrupted work recovered"
        case .continuationStarted:      return "Picking up where it left off"
        case .continuationCompleted:    return "Carried on from where it left off"
        case .desktopObserved:          return event.text("summary") ?? "Noticed something on your desktop"
        // Tasks
        case .taskPhaseStarted:   return "Phase started: \(event.text("name") ?? "the next part")"
        case .taskPhaseCompleted: return "Phase done: \(event.text("name") ?? "that part")"
        case .taskDecision:       return "Decided: \(event.text("decision") ?? "how to proceed")"
        case .taskArtifact:       return "Produced a file: \(event.text("filename") ?? "an artifact")"
        case .taskHealthChanged:  return "Task health is now \(event.text("new_health") ?? "different")"
        case .taskRepeatedFailure:
            return "The same step keeps failing: \(event.text("tool") ?? "something isn't working")"
        case .taskCompletionRejected:
            return "Review sent the task back: \(event.text("summary") ?? "it isn't done yet")"
        case .taskUserAnswered:   return "Your answer got the task moving again"
        case .taskCancelled:      return "Task cancelled\(event.text("reason").map { ": \($0)" } ?? "")"
        case .taskSuperseded:     return "Task replaced by a newer one"
        case .taskModified:
            // Turn 6: payload carries `action` (add/remove/replace/reset/edit_step/change_objective)
            // + step or objective. Render the change in a human line without dumping the JSON.
            let action = event.text("action") ?? "changed"
            if let objective = event.text("objective") {
                return "Renamed task: \(objective)"
            }
            if let step = event.text("step"), let desc = event.text("description") {
                return "Step \(step) \(action == "add" ? "added" : action == "remove" ? "removed" : "updated"): \(desc)"
            }
            if let step = event.text("step") {
                return "Step \(step) \(action == "remove" ? "removed" : "reset")"
            }
            return "Task plan updated"
        // Tools & research
        case .toolFailed:      return "A step failed: \(event.text("tool") ?? "a tool")"
        case .toolDenied:      return "Blocked a step: \(event.text("reason") ?? "not allowed")"
        case .toolUnavailable: return "Reached for a tool it doesn't have: \(event.text("name") ?? "unknown")"
        case .researchFailed:  return "Research came up empty: \(event.text("query") ?? "no answer found")"
        case .researchUsed:    return "Used what it had already researched"
        // Proactive / run lifecycle
        case .reengagement:    return "Came back to an open question"
        case .runInterrupted:  return "A run was interrupted"
        case .runError:        return "A background run hit an error"
        // Persistent-organism autonomy
        case .openLoopCreated:      return "Noticed something to follow up on: \(event.text("title") ?? "an open loop")"
        case .openLoopReinforced:   return "Revisited an open loop: \(event.text("title") ?? "a follow-up")"
        case .openLoopResolved:     return "Resolved an open loop"
        case .openLoopDismissed:    return "Dismissed an open loop"
        case .curiosityCreated:     return "Wondered about: \(event.text("subject") ?? "something new")"
        case .curiosityReinforced:  return "Kept thinking about: \(event.text("subject") ?? "that")"
        case .curiosityLearned:     return "Learned about: \(event.text("subject") ?? "something")"
        case .curiosityResolved:    return "Wrapped up thinking about: \(event.text("subject") ?? "it")"
        case .proactiveMessageSent: return "Sent you a message: \(event.text("kind") ?? "note")"
        case .proactiveMessageSuppressed:
            return "Chose silence: \(event.text("kind") ?? "would have been noise")"
        case .initiativeDriverCycle: return "Considered what to work on next"
        case .runtimeBackgroundWedged:
            return "A background loop wedged briefly: \(event.text("faculty") ?? "one faculty")"
        case .skillProposalCreated:  return "Proposed a skill improvement: \(event.text("skill") ?? "a skill")"
        case .skillProposalAccepted: return "Accepted a skill improvement"
        case .skillProposalRejected: return "Rejected a skill improvement"
        case .unknown:         return event.rawType
        }
    }

    // MARK: - Presentation classification (kept out of the views so every surface agrees)

    /// The broad area this event belongs to — what a row's icon should say.
    public enum Group: Sendable, Equatable, Hashable {
        case goal, initiative, promise, activity, worldAction, routine
        case learning, behavior, capability, memory, consent
        case attention, runtime, task, tool, research, proactive, unknown
    }

    /// How the event landed — what a row's tint should say. Deliberately coarse: the app states outcomes,
    /// it does not editorialise.
    public enum Outcome: Sendable, Equatable, Hashable {
        case neutral, good, needsAttention, bad
    }

    public var group: Group {
        switch self {
        case .goalCreated, .goalUpdated, .goalCompleted, .goalBlocked: return .goal
        case .initiativeCreated, .initiativeSelected, .initiativeCompleted,
             .initiativeBlocked, .initiativeDeferred: return .initiative
        case .obligationCreated, .obligationResolved, .obligationReopened,
             .commitmentCreated, .commitmentFulfilled, .commitmentCancelled, .commitmentOverdue: return .promise
        case .activityStarted, .activityProgressed, .activityCompleted,
             .activityFailed, .activityBlocked, .activityResumed: return .activity
        case .digitalActionStarted, .digitalActionProgressed, .digitalActionVerified,
             .digitalActionWaiting, .digitalActionBlocked, .digitalActionFailed,
             .sideEffectPlanned, .sideEffectSucceeded, .sideEffectFailed, .sideEffectRecovered,
             .intentRevocationPropagated: return .worldAction
        case .routineCompleted, .routineFailed: return .routine
        case .learningObserved, .learningCandidate, .learningVerified, .learningPromoted, .learningRejected,
             .learningContradicted, .learningSuperseded, .learningReflection, .learningProcedure,
             .learningToolExperience, .learningFromFailure, .learningDailyStarted,
             .learningDailyCompleted, .learningDailyFailed: return .learning
        case .behaviorProposed, .behaviorTesting, .behaviorAccepted,
             .behaviorRejected, .behaviorSuperseded: return .behavior
        case .capabilityDiscovered, .capabilityVerified, .capabilityAttempted, .capabilityUsed,
             .capabilityDegraded, .capabilityFailed: return .capability
        case .experienceRecorded, .experienceVerified, .experienceLesson, .memoryConsolidated,
             .memoryRetrieved, .knowledgeRetrieved, .twinSynced, .twinEntityAdded,
             .twinEntityRemoved: return .memory
        case .consentRequested, .consentGranted, .consentModified,
             .consentRevoked, .consentExpired: return .consent
        case .attentionInterrupted, .attentionCancelled, .attentionResumed: return .attention
        case .contextLimitApproaching, .contextCompactionStarted, .contextCompactionCompleted,
             .contextCompactionFailed, .contextOverflowRecovered, .contextEmergencyMode,
             .runtimeRecoveryStarted, .runtimeRecoveryCompleted, .continuationStarted,
             .continuationCompleted, .desktopObserved, .runInterrupted, .runError: return .runtime
        case .taskPhaseStarted, .taskPhaseCompleted, .taskDecision, .taskArtifact, .taskHealthChanged,
             .taskRepeatedFailure, .taskCompletionRejected, .taskUserAnswered,
             .taskCancelled, .taskSuperseded, .taskModified: return .task
        case .toolFailed, .toolDenied, .toolUnavailable: return .tool
        case .researchFailed, .researchUsed: return .research
        case .reengagement: return .proactive
        case .openLoopCreated, .openLoopReinforced, .openLoopResolved, .openLoopDismissed:
            return .proactive
        case .curiosityCreated, .curiosityReinforced, .curiosityLearned, .curiosityResolved:
            return .learning
        case .proactiveMessageSent, .proactiveMessageSuppressed: return .proactive
        case .initiativeDriverCycle: return .initiative
        case .runtimeBackgroundWedged: return .runtime
        case .skillProposalCreated, .skillProposalAccepted, .skillProposalRejected: return .behavior
        case .unknown: return .unknown
        }
    }

    public var outcome: Outcome {
        switch self {
        case .goalCompleted, .initiativeCompleted, .obligationResolved, .commitmentFulfilled,
             .activityCompleted, .digitalActionVerified, .sideEffectSucceeded, .routineCompleted,
             .learningVerified, .learningPromoted, .behaviorAccepted, .capabilityDiscovered,
             .capabilityVerified, .experienceVerified, .consentGranted, .contextCompactionCompleted,
             .contextOverflowRecovered, .runtimeRecoveryCompleted, .taskPhaseCompleted,
             .taskArtifact, .taskUserAnswered:
            return .good
        case .goalBlocked, .initiativeBlocked, .commitmentOverdue, .activityBlocked, .obligationReopened,
             .digitalActionWaiting, .digitalActionBlocked, .learningContradicted, .capabilityDegraded,
             .consentRequested, .consentExpired, .consentRevoked, .attentionInterrupted,
             .attentionCancelled, .contextLimitApproaching, .contextEmergencyMode, .taskHealthChanged,
             .taskCompletionRejected, .toolDenied, .behaviorProposed:
            return .needsAttention
        case .activityFailed, .digitalActionFailed, .sideEffectFailed, .routineFailed,
             .learningDailyFailed, .capabilityFailed, .contextCompactionFailed, .taskRepeatedFailure,
             .toolFailed, .toolUnavailable, .researchFailed, .runError,
             .runtimeBackgroundWedged:
            return .bad
        case .openLoopResolved, .curiosityLearned, .curiosityResolved, .skillProposalAccepted:
            return .good
        case .openLoopCreated, .skillProposalCreated:
            return .needsAttention
        default:
            return .neutral
        }
    }
}

/// A minimal JSON value so event `data` decodes without a schema per event type.
public enum JSONValue: Sendable, Equatable {
    case string(String), number(Double), bool(Bool), null
    case array([JSONValue]), object([String: JSONValue])
}

extension JSONValue: Decodable {
    public init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let b = try? c.decode(Bool.self) { self = .bool(b) }
        else if let n = try? c.decode(Double.self) { self = .number(n) }
        else if let s = try? c.decode(String.self) { self = .string(s) }
        else if let a = try? c.decode([JSONValue].self) { self = .array(a) }
        else if let o = try? c.decode([String: JSONValue].self) { self = .object(o) }
        else { self = .null }
    }
}

extension SaliEvent {
    /// Decode one WebSocket frame into a SaliEvent (best-effort; unknown frames become `.other(.unknown)`).
    static func decode(from json: [String: Any]) -> SaliEvent? {
        let frameType = json["type"] as? String ?? ""
        // Non-event control frames (connected/subscribed/pong) are surfaced too so the UI can react.
        let rawType = (json["event_type"] as? String) ?? frameType
        var bag: [String: JSONValue] = [:]
        if let d = json["data"] as? [String: Any] {
            for (k, v) in d { bag[k] = wrap(v) }
        }
        let ts = json["timestamp"]
        return SaliEvent(
            id: (json["event_id"] as? String) ?? UUID().uuidString,
            type: EventType(rawType: rawType),
            rawType: rawType,
            sequence: json["sequence"] as? Int,
            timestamp: parseDate(ts),
            taskId: json["task_id"] as? String,
            runId: json["run_id"] as? String,
            sessionId: json["session_id"] as? String,
            origin: json["origin"] as? String,
            data: bag,
            replayed: (json["replayed"] as? Bool) ?? false
        )
    }

    /// Build an event from one row of `GET /api/v1/events` — the durable log the Activity center is seeded
    /// from on a cold start. The row is flattened EXACTLY the way `ConnectionManager.replay_since` flattens
    /// it for the socket (src/sali/api/ws.py): the envelope keys are lifted out of `payload` and whatever
    /// remains is the event's own `data`, so a seeded event and a live one are indistinguishable downstream.
    ///
    /// Marked `replayed` because that is what it is: history, not something happening now.
    static func durable(rawType: String, sequence: Int?, timestamp: String?,
                        payload: [String: JSONValue]) -> SaliEvent {
        func lifted(_ key: String) -> String? {
            if case let .string(s)? = payload[key] { return s.isEmpty ? nil : s }
            return nil
        }
        let envelope: Set<String> = ["event_id", "run_id", "task_id", "session_id", "origin"]
        var bag: [String: JSONValue] = [:]
        for (k, v) in payload where !envelope.contains(k) { bag[k] = v }
        return SaliEvent(
            // The REST row exposes `seq` but not the event table's UUID, so a row whose payload didn't carry
            // an `event_id` is keyed by its sequence — stable and unique, never a made-up identifier.
            id: lifted("event_id") ?? sequence.map { "seq-\($0)" } ?? UUID().uuidString,
            type: EventType(rawType: rawType),
            rawType: rawType,
            sequence: sequence,
            timestamp: parseDate(timestamp),
            taskId: lifted("task_id"),
            runId: lifted("run_id"),
            sessionId: lifted("session_id"),
            origin: lifted("origin"),
            data: bag,
            replayed: true
        )
    }

    private static func wrap(_ v: Any) -> JSONValue {
        switch v {
        case let s as String: .string(s)
        case let b as Bool: .bool(b)
        case let n as Double: .number(n)
        case let i as Int: .number(Double(i))
        case let a as [Any]: .array(a.map(wrap))
        case let o as [String: Any]: .object(o.mapValues(wrap))
        default: .null
        }
    }

    private static func parseDate(_ v: Any?) -> Date? {
        if let d = v as? Double { return Date(timeIntervalSince1970: d) }
        if let s = v as? String {
            let isoFrac = ISO8601DateFormatter()
            isoFrac.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            if let date = isoFrac.date(from: s) { return date }
            let iso = ISO8601DateFormatter()
            iso.formatOptions = [.withInternetDateTime]
            if let date = iso.date(from: s) { return date }
            // Postgres renders `created_at` with microsecond precision ("…T12:00:00.123456+00:00"), which
            // ISO8601DateFormatter refuses. Trim the sub-millisecond digits rather than dropping the
            // timestamp — a row with no time can't be ordered against the live stream.
            if let dot = s.firstIndex(of: "."), s.count > 4 {
                let tail = s[s.index(dot, offsetBy: 1)...]
                let digits = tail.prefix(while: \.isNumber)
                if digits.count > 3 {
                    let trimmed = s.replacingOccurrences(of: ".\(digits)", with: ".\(digits.prefix(3))")
                    return isoFrac.date(from: trimmed)
                }
            }
            return nil
        }
        return nil
    }
}
