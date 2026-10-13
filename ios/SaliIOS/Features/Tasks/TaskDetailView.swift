import SwiftUI

// Full task inspection (Prompt 13, §14/§42.5) — pushed from `TasksView`, so (like `NotificationsView`) this
// does not open its own `NavigationStack`.
//
// THE SHAPE OF THE SCREEN, top to bottom:
//
//   ┌ pinned ────────────────────────────────────────────────┐
//   │ vitals: objective · phase · live step · rail · health   │  never scrolls away
//   │ "waiting on you" bar (only when the task is parked)     │
//   ├ scrolling ─────────────────────────────────────────────┤
//   │ while you were away  ·  output  ·  verification         │
//   │ THE TIMELINE (the centrepiece)                          │
//   │ pulse  ·  provenance drawer                             │
//   ├ pinned ────────────────────────────────────────────────┤
//   │ answer composer, anchored to the keyboard               │  only when parked on you
//   └────────────────────────────────────────────────────────┘
//
// The core `TaskRecord` anchors the screen; workspace/skills/research/reviews/phases are supplementary and
// degrade on their own — a hiccup fetching "reviews" never blanks the timeline or the controls.
//
// Nothing here infers completion from prose. `result` can be written while a task is still running, and the
// reviewer (`GET /tasks/{id}/reviews`) is the only authority on "done" — so Output says what was recorded,
// Verification says who checked it, and the two are never merged into a single green tick.

// MARK: - Supplementary endpoint models

/// `GET /api/v1/tasks/{id}/workspace` — authoritative workspace roots + mode. Every field optional: an
/// unrecognized or partial shape still decodes to "nothing recorded" rather than throwing away the screen.
struct WorkspaceDetail: Decodable, Sendable {
    let workspaceRoot: String?
    let workspaceMode: String?
    let roots: [String]?

    enum CodingKeys: String, CodingKey {
        case workspaceRoot = "workspace_root", workspaceMode = "workspace_mode", roots
    }
}

/// `GET /api/v1/tasks/{id}/skills` → `{skills:[{name,path,content_hash,score}]}`.
private struct SkillsResponse: Decodable, Sendable { let skills: [SkillSnapshot] }

struct SkillSnapshot: Decodable, Identifiable, Sendable {
    var id: String { path }
    let name: String
    let path: String
    let contentHash: String?
    let score: Double?

    enum CodingKeys: String, CodingKey { case name, path, contentHash = "content_hash", score }
}

/// `GET /api/v1/tasks/{id}/research` → `{research:[...], candidates:[...]}`. Item shapes aren't pinned down
/// by the API reference beyond "counts + brief", so every field here is optional and a sensible fallback
/// chain produces a one-line brief without inventing content.
struct ResearchSummary: Decodable, Sendable {
    let research: [ResearchEntry]
    let candidates: [ResearchEntry]
}

struct ResearchEntry: Decodable, Identifiable, Sendable {
    var id: String { identifier ?? UUID().uuidString }
    let identifier: String?
    let summary: String?
    let title: String?
    let query: String?

    enum CodingKeys: String, CodingKey { case identifier = "id", summary, title, query }

    var brief: String { summary ?? title ?? query ?? "Untitled" }
}

/// `GET /api/v1/tasks/{id}/reviews` → `{attempts, reviewer_status, latest, reviews}`.
struct TaskReviewsResponse: Decodable, Sendable {
    let attempts: Int?
    let reviewerStatus: String?
    let latest: ReviewEntry?
    let reviews: [ReviewEntry]?

    enum CodingKeys: String, CodingKey {
        case attempts, reviewerStatus = "reviewer_status", latest, reviews
    }
}

struct ReviewEntry: Decodable, Identifiable, Sendable {
    // Each review attempt has a distinct `attempt` number → a stable, non-colliding id.
    var id: String { "attempt-\(sequence ?? 0)" }
    let sequence: Int?
    let verdict: String?
    let reason: String?
    let createdAt: Date?

    // The backend (TaskReviewer.to_public, src/sali/tasks/reviewer.py) sends `attempt`, `status`, `summary`
    // — NOT sequence/verdict/reason. Map to the real keys; `created_at` isn't sent, so it stays nil.
    enum CodingKeys: String, CodingKey {
        case sequence = "attempt", verdict = "status", reason = "summary", createdAt = "created_at"
    }
}

/// `GET /api/v1/tasks/{id}/phases` → `{task_id, phases:[{seq,name,status,summary}]}` (src/sali/tasks/
/// ledger.py `PhaseStore.phases`). The altitude between "objective" and "step 12 of 31": a long task moves
/// through named stages, and exactly one of them is `active` at a time. Phases are created as they START,
/// so there is never a future phase to render — the ribbon shows the road travelled and where Sali is on it,
/// and never guesses at what comes after.
struct TaskPhase: Decodable, Identifiable, Sendable {
    var id: Int { seq }
    let seq: Int
    let name: String
    let status: String        // active | done
    let summary: String?

    var isActive: Bool { status == "active" }
}

private struct TaskPhasesResponse: Decodable, Sendable { let phases: [TaskPhase] }

/// `GET /api/v1/tasks/{id}/question` → `{task_id, pending: {id, question} | null}` — the durable
/// clarification a `waiting_for_user` task is parked on (§44). `pending` is null far more often than not,
/// and the route 404s for a task that no longer exists; both mean "nothing to answer", never an error.
private struct TaskQuestionResponse: Decodable, Sendable { let pending: TaskQuestion? }

struct TaskQuestion: Decodable, Identifiable, Sendable {
    let id: String
    let question: String
}

/// `POST /api/v1/tasks/{id}/question/answer` → `{status:"answered", task_id}`.
private struct TaskAnswerAck: Decodable, Sendable { let status: String }

/// `POST /api/v1/pending-questions/answer` — the person-directed question table the Life screen uses. It is
/// the FALLBACK here, not the primary route: a task clarification lives in `task_question`, which only the
/// task-scoped route above can answer.
private struct TaskAnswerRouting: Decodable, Sendable {
    let status: String                  // answered | not_waiting | ambiguous | no_pending_question
    let candidates: [TaskAnswerCandidate]?
}
struct TaskAnswerCandidate: Decodable, Identifiable, Hashable, Sendable {
    let id: String
    let question: String
}

/// One row of `GET /api/v1/events` — the durable event log the pulse lane sits on. Decoded like the Activity
/// feed's seed, plus the envelope's `subject_id`, which this screen genuinely needs.
///
/// Not every emitter goes through `EventPublisher`: the direct-SQL paths (`store.py::_emit_task`'s fallback,
/// `watchdog.py`'s fallback, and `recovery.py::mark_task_interrupted`) write `subject_type='task',
/// subject_id=<task id>` and a payload with NO `task_id` key. `task.interrupted` — the one event that says
/// "the process died under this task" — is emitted exactly that way, so a pulse keyed only on the payload's
/// `task_id` would silently drop it. For `subject_type == "task"`, `subject_id` IS the task id (the
/// publisher itself falls back to it: `subject_id or event.task_id`), so it is lifted into the payload
/// before decoding rather than inventing a second identity rule downstream.
private struct DurableTaskEvent: Decodable, Sendable {
    let seq: Int?
    let type: String?
    let payload: [String: JSONValue]?
    let timestamp: String?
    let subjectType: String?
    let subjectId: String?

    enum CodingKeys: String, CodingKey {
        case seq, type, payload, timestamp
        case subjectType = "subject_type", subjectId = "subject_id"
    }

    var event: SaliEvent? {
        guard let type, !type.isEmpty else { return nil }
        var bag = payload ?? [:]
        if bag["task_id"] == nil, subjectType == "task", let subjectId, !subjectId.isEmpty {
            bag["task_id"] = .string(subjectId)
        }
        return SaliEvent.durable(rawType: type, sequence: seq, timestamp: timestamp, payload: bag)
    }
}

// MARK: - While you were away

/// What changed on THIS task since the last time this screen was open. Built only from events that carry a
/// real timestamp, against a last-seen stamp kept locally (`TaskSeenStore`) — never from a diff the app
/// imagined between two renders.
struct TaskAwayDigest: Equatable, Sendable {
    let since: Date
    let lines: [String]

    /// - Parameter events: task-scoped events, NEWEST FIRST (as `TaskFeed.merge` returns them).
    static func build(events: [SaliEvent], since: Date) -> TaskAwayDigest? {
        let fresh = events.filter { ($0.timestamp ?? .distantPast) > since }
        guard !fresh.isEmpty else { return nil }

        func count(_ rawType: String) -> Int { fresh.filter { $0.rawType == rawType }.count }
        func newest(_ rawType: String) -> SaliEvent? { fresh.first { $0.rawType == rawType } }
        func plural(_ n: Int, _ word: String) -> String { "\(n) \(word)\(n == 1 ? "" : "s")" }

        var lines: [String] = []
        let stepsDone = count("task.step.completed")
        if stepsDone > 0 { lines.append("\(plural(stepsDone, "step")) completed") }
        let stepsFailed = count("task.step.failed")
        if stepsFailed > 0 { lines.append("\(plural(stepsFailed, "step")) failed") }
        if let phase = newest("task.phase_started")?.text("name") {
            lines.append("Phase “\(phase)” started")
        }
        let files = count("task.artifact.created")
        if files > 0 { lines.append("\(plural(files, "file")) produced") }
        if newest("task.interrupted") != nil { lines.append("Interrupted mid-run") }
        if newest("task.resumed") != nil { lines.append("Picked up again after a stop") }
        if newest("task.waiting_for_user") != nil { lines.append("Parked on a question for you") }
        if newest("task.completion_rejected") != nil { lines.append("The reviewer sent the work back") }
        if let health = newest("task.health_changed")?.text("new_health") {
            lines.append("Health changed to \(health.replacingOccurrences(of: "_", with: " "))")
        }
        if newest("task.finished") != nil { lines.append("The task finished") }

        if lines.isEmpty {
            // Real activity, none of it in the categories above. Say how much and let the pulse say what —
            // better than inventing a headline for events this build doesn't summarise.
            lines.append("\(plural(fresh.count, "update")) on this task")
        }
        return TaskAwayDigest(since: since, lines: lines)
    }
}

// MARK: - View model

@MainActor
final class TaskDetailViewModel: ObservableObject {
    let taskId: String

    @Published var record: Loadable<TaskRecord> = .idle
    @Published private(set) var phases: [TaskPhase] = []
    @Published private(set) var workspace: WorkspaceDetail?
    @Published private(set) var skills: [SkillSnapshot] = []
    @Published private(set) var research: ResearchSummary?
    @Published private(set) var reviews: TaskReviewsResponse?
    @Published private(set) var resources: ResourceSnapshot?
    @Published var artifacts: Loadable<[ArtifactMeta]> = .idle

    /// The durable event history the pulse lane sits on, already scoped to this task.
    @Published private(set) var pulseSeed: [SaliEvent] = []
    /// Highest `seq` this task's pulse has ingested from the durable log. Reappearances
    /// gap-fill via `/tasks/{id}/events?after_seq=N` instead of re-scanning the global tail.
    /// Per-VM (VM is per-task), so no cross-task leakage.
    private var pulseAfterSeq: Int = 0

    /// Read ONCE, before this visit updates it — otherwise "while you were away" would always be empty.
    @Published private(set) var lastSeen: Date?
    @Published var awayDismissed = false

    @Published var isPerformingControl = false
    @Published var controlError: String?

    // Inline clarification (§44 / §35) — a task parked on the user, answered right here.
    @Published private(set) var pendingQuestion: TaskQuestion?
    @Published private(set) var ambiguousCandidates: [TaskAnswerCandidate] = []
    @Published var answerDraft = ""
    @Published private(set) var isSendingAnswer = false
    @Published var answerError: String?

    @Published private(set) var downloadingArtifactId: String?
    @Published private(set) var downloadedFiles: [String: URL] = [:]
    @Published var downloadError: String?

    init(taskId: String) { self.taskId = taskId }

    var task: TaskSummary? { record.value?.task }
    var facts: TaskFacts { record.value?.facts ?? .unknown }

    /// The active phase, plus how many there have been — the ribbon's headline.
    var currentPhase: TaskPhase? { phases.last { $0.isActive } ?? phases.last }

    /// Captured before the screen marks itself seen, so the digest has something to compare against.
    func captureLastSeen() {
        guard lastSeen == nil else { return }
        lastSeen = TaskSeenStore.lastSeen(taskId)
    }

    func markSeen() { TaskSeenStore.markSeen(taskId) }

    // MARK: Event-driven refresh — coalesced, and scoped to what the frame actually changed
    //
    // This is where a task is WATCHED, so it has to react to every frame the backend sends. But
    // `task.progress` is emitted on every tool call, and the old rule — `loadAll` per frame — meant NINE
    // requests per frame, which is how "live" became "janky". A burst now settles into ONE refresh, and a
    // frame only pays for what it invalidates: a step landing re-reads the task, a produced file re-reads
    // the artifacts, a lifecycle change re-reads everything. Still purely event-driven; no timer (§40).

    private var refreshTask: Task<Void, Never>?
    private var refreshPending = false
    private var refreshScope: RefreshScope = .task
    /// How long a burst of frames is allowed to settle before it costs a request.
    private static let settle: Duration = .milliseconds(250)

    /// How much of the screen a frame invalidates. Ordered, so the widest scope in a burst wins.
    private enum RefreshScope: Int, Comparable {
        case task = 0         // the record and its steps
        case artifacts = 1    // …and the files this task has produced
        case everything = 2   // …and phases, reviews, workspace, the pending question
        static func < (lhs: Self, rhs: Self) -> Bool { lhs.rawValue < rhs.rawValue }
    }

    /// The frames that change something OTHER than the task row — a phase, a verdict, a question, or the
    /// end of the task itself.
    private static let lifecycleEvents: Set<String> = [
        "task.created", "task.activated", "task.finished", "task.suspended", "task.resumed",
        "task.interrupted", "task.waiting_for_user", "task.user_answered", "task.phase_started",
        "task.phase_completed", "task.completion_rejected", "task.decision_created",
        "task.decision_superseded", "task.cancelled", "task.superseded", "task.health_changed",
        "intent.revoked",
    ]

    /// One entry point for every live frame. Matching on the RAW event name is deliberate: the task
    /// lifecycle the backend really emits is wider than the six cases the shared `EventType` models —
    /// `task.artifact.created`, `task.phase_started`, `task.interrupted` and `task.health_changed` all
    /// collapse into `.other`, so an allowlist of enum cases silently ignored them and this screen went
    /// stale exactly when it mattered.
    /// Turn 5 catch-up hook. AppState's `replayCaughtUpTick` bumps after a WebSocket reconnect
    /// completes each replay batch; this ensures the detail is re-read at the widest scope so
    /// state that changed while offline (task finished, artifact appeared, phase advanced) is
    /// picked up without pull-to-refresh. Routes through the same 250 ms `schedule(.everything)`
    /// coalescer, so multi-batch replays collapse into one reload.
    func onReplayCaughtUp(api: APIClient) {
        schedule(.everything, api: api)
        // The pulse seed also gap-fills from the durable log — `after_seq=<watermark>` means
        // we only ask for what we missed, so a long offline gap is a small delta not a re-scan.
        Task { await seedPulse(api: api) }
    }

    func handle(event: SaliEvent, api: APIClient) {
        guard TasksViewModel.isTaskEvent(event) else { return }
        // Live and replayed frames both carry a top-level `task_id`; a frame that omits it is "can't tell"
        // and is treated as ours rather than dropped.
        guard event.taskId == nil || event.taskId == taskId else { return }
        let scope: RefreshScope
        if Self.lifecycleEvents.contains(event.rawType) {
            scope = .everything
        } else if event.rawType == "task.artifact.created" {
            scope = .artifacts
        } else {
            scope = .task
        }
        schedule(scope, api: api)
    }

    private func schedule(_ scope: RefreshScope, api: APIClient) {
        refreshScope = max(refreshScope, scope)
        refreshPending = true
        guard refreshTask == nil else { return }
        refreshTask = Task { [weak self] in
            defer { self?.refreshTask = nil }
            // Loops rather than returns, so frames that arrive DURING a refresh cost exactly one more.
            while let self, self.refreshPending {
                try? await Task.sleep(for: Self.settle)
                self.refreshPending = false
                let scope = self.refreshScope
                self.refreshScope = .task
                switch scope {
                case .task:
                    await self.loadTask(api: api)
                case .artifacts:
                    async let record: () = self.loadTask(api: api)
                    async let files: () = self.loadArtifacts(api: api)
                    _ = await (record, files)
                case .everything:
                    await self.loadAll(api: api)
                }
            }
        }
    }

    func loadAll(api: APIClient) async {
        async let taskLoad: () = loadTask(api: api)
        async let phasesLoad: () = loadPhases(api: api)
        async let workspaceLoad: () = loadWorkspace(api: api)
        async let skillsLoad: () = loadSkills(api: api)
        async let researchLoad: () = loadResearch(api: api)
        async let reviewsLoad: () = loadReviews(api: api)
        async let resourcesLoad: () = loadResources(api: api)
        async let artifactsLoad: () = loadArtifacts(api: api)
        async let pulseLoad: () = seedPulse(api: api)
        _ = await (taskLoad, phasesLoad, workspaceLoad, skillsLoad, researchLoad,
                   reviewsLoad, resourcesLoad, artifactsLoad, pulseLoad)
        // Sequenced after the fan-out because the fetch is only meaningful once we know the task is waiting.
        await loadQuestion(api: api)
    }

    /// A clarification only exists while the task is parked on the user, so the fetch is gated on the state
    /// we just loaded. A 404, a decode miss, or `pending: null` all collapse to "nothing to answer" — the
    /// bar simply isn't shown; there is deliberately no error surface for it.
    private func loadQuestion(api: APIClient) async {
        guard let state = task?.state, state == .waiting || state == .waitingForUser else {
            pendingQuestion = nil
            ambiguousCandidates = []
            return
        }
        let response: TaskQuestionResponse? = try? await api.get("tasks/\(taskId)/question")
        pendingQuestion = response?.pending
    }

    private func loadTask(api: APIClient) async {
        if case .loaded = record {} else { record = .loading }
        do {
            let value: TaskRecord = try await api.get("tasks/\(taskId)")
            record = .loaded(value)
        } catch {
            // Live event-driven refreshes are frequent, so a transient failure must never blank a task the
            // user is already watching — the same rule `TasksViewModel` applies to the list.
            if record.value == nil {
                record = .failed((error as? APIError)?.errorDescription ?? "Couldn't load this task.")
            }
        }
    }

    private func loadPhases(api: APIClient) async {
        let response: TaskPhasesResponse? = try? await api.get("tasks/\(taskId)/phases")
        phases = (response?.phases ?? []).sorted { $0.seq < $1.seq }
    }

    private func loadWorkspace(api: APIClient) async {
        workspace = try? await api.get("tasks/\(taskId)/workspace")
    }

    private func loadSkills(api: APIClient) async {
        let response: SkillsResponse? = try? await api.get("tasks/\(taskId)/skills")
        skills = response?.skills ?? []
    }

    private func loadResearch(api: APIClient) async {
        research = try? await api.get("tasks/\(taskId)/research")
    }

    private func loadReviews(api: APIClient) async {
        reviews = try? await api.get("tasks/\(taskId)/reviews")
    }

    /// Best-effort — connects "why is this slow/paused" to the same resource ladder the System screen shows,
    /// without inventing a per-task field the backend doesn't have (§13 "connect metrics to meaning").
    private func loadResources(api: APIClient) async {
        resources = try? await api.get("resources")
    }

    /// A SEED, not a poll (§40): the durable log is read once per refresh and everything newer arrives on
    /// the live socket. Filtered to this task here so the lane can never show another task's work.
    private func seedPulse(api: APIClient) async {
        // Turn 5: task-scoped route so a busy backend catch-up doesn't drop this task's
        // events. Was: /events?limit=200 with client-side filter — the first 200 could all
        // belong to other tasks. Now: /tasks/{id}/events UNION on (subject_id=task) OR
        // (payload.task_id=task) returns ONLY this task's rows, bounded by limit.
        // Watermark: after the first seed we only ask for rows past our high-water seq,
        // so re-appearances (tab switch, foreground) are a small gap-fill not a re-scan.
        var query: [String: String] = ["limit": "200"]
        if pulseAfterSeq > 0 { query["after_seq"] = String(pulseAfterSeq) }
        let rows: [DurableTaskEvent]? = try? await api.get(
            "tasks/\(taskId)/events", query: query)
        guard let rows else { return }
        let events = rows.compactMap(\.event)
        // Merge with what's already there (in case a live-socket frame arrived DURING the
        // fetch); dedupe by sequence + sort ascending so downstream consumers see chronology.
        // Events with a nil sequence (rare) fall to the tail with stable ordering preserved.
        var merged = pulseSeed
        let seenSeq: Set<Int> = Set(merged.compactMap(\.sequence))
        for e in events {
            if let s = e.sequence, seenSeq.contains(s) { continue }
            merged.append(e)
        }
        merged.sort { ($0.sequence ?? Int.max) < ($1.sequence ?? Int.max) }
        pulseSeed = merged
        if let maxSeq = events.compactMap(\.sequence).max(), maxSeq > pulseAfterSeq {
            pulseAfterSeq = maxSeq
        }
    }

    func loadArtifacts(api: APIClient) async {
        if case .loaded = artifacts {} else { artifacts = .loading }
        do {
            let list: [ArtifactMeta] = try await api.get("tasks/\(taskId)/artifacts")
            artifacts = .loaded(list)
        } catch {
            artifacts = .failed((error as? APIError)?.errorDescription ?? "Couldn't load artifacts.")
        }
    }

    // MARK: Controls (owner/controller only, gated in the view)

    func pause(api: APIClient) async { await control(api: api, action: "pause") }
    func resume(api: APIClient) async { await control(api: api, action: "resume") }

    @discardableResult
    func abandon(api: APIClient) async -> Bool { await control(api: api, action: "abandon") }

    @discardableResult
    private func control(api: APIClient, action: String) async -> Bool {
        isPerformingControl = true
        defer { isPerformingControl = false }
        do {
            try await api.postVoid("tasks/\(taskId)/\(action)")
            // Turn 5: /cancel now = revoke_intent (matches /abandon since Turn 2). Both write
            // a tombstone + archive the row synchronously, so a follow-up loadTask races the
            // publisher and can 404 on the intent that was just revoked. Skip the reload for
            // both; the incoming intent.revoked event will drive the view's next refresh.
            if action != "abandon" && action != "cancel" {
                await loadTask(api: api)
            }
            return true
        } catch {
            controlError = (error as? APIError)?.errorDescription ?? "Couldn't complete that action."
            return false
        }
    }

    // MARK: Answering (§35 — a free-text reply, never a y/n gate)

    /// Answers the TASK's own clarification first: `GET /tasks/{id}/question` reads the `task_question`
    /// table (QuestionStore), and `POST /tasks/{id}/question/answer` is the only route that writes it —
    /// which is why answering through the person-directed `/pending-questions/answer` route used to come
    /// back "no_pending_question" and leave the task parked. That route is kept as a FALLBACK, for the case
    /// where what is really waiting is a person-directed question routed by text (the Life screen's flow,
    /// including its ambiguity resolution).
    func submitAnswer(_ text: String, api: APIClient) async {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        isSendingAnswer = true
        defer { isSendingAnswer = false }

        do {
            let ack: TaskAnswerAck = try await api.post(
                "tasks/\(taskId)/question/answer", json: ["answer": trimmed])
            if ack.status == "answered" {
                ambiguousCandidates = []
                answerDraft = ""
                await loadAll(api: api)
                return
            }
        } catch {
            let isMissing = (error as? APIError).map { if case .notFound = $0 { true } else { false } } ?? false
            guard isMissing else {
                answerError = (error as? APIError)?.errorDescription ?? "Couldn't send that just now."
                return
            }
            // 404 = this task has no clarification of its own → try the person-directed table below.
        }

        await submitToPendingQuestions(trimmed, api: api)
    }

    private func submitToPendingQuestions(_ trimmed: String, api: APIClient) async {
        do {
            let result: TaskAnswerRouting = try await api.post(
                "pending-questions/answer", json: ["answer": trimmed])
            switch result.status {
            case "answered":
                ambiguousCandidates = []
                answerDraft = ""
                await loadAll(api: api)
            case "ambiguous":
                ambiguousCandidates = result.candidates ?? []
            case "no_pending_question", "not_waiting":
                answerError = "That question isn't waiting on you anymore."
                await loadAll(api: api)
            default:
                answerError = "Couldn't send that just now."
            }
        } catch {
            answerError = (error as? APIError)?.errorDescription ?? "Couldn't send that just now."
        }
    }

    /// No by-ID answer endpoint exists on the fallback route (routing matches on text only), so picking a
    /// candidate nudges the reply toward it — richer, more specific text — rather than silently "selecting".
    func seedAmbiguousReply(with candidate: TaskAnswerCandidate) {
        let trimmed = answerDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        answerDraft = trimmed.isEmpty
            ? "Re: \(candidate.question) — "
            : "Re: \(candidate.question) — \(trimmed)"
    }

    // MARK: Artifact download → ShareLink

    /// Downloads the artifact's bytes and writes them into the app's own sandbox temp directory, keyed only
    /// by its own filename — the server-recorded host path is never requested or displayed (§9/§43).
    func download(_ artifact: ArtifactMeta, api: APIClient) async {
        downloadingArtifactId = artifact.id
        defer { downloadingArtifactId = nil }
        do {
            let (data, _) = try await api.download(relativePath: artifact.downloadURL)
            let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            let fileURL = directory.appendingPathComponent(artifact.filename)
            try data.write(to: fileURL, options: .atomic)
            downloadedFiles[artifact.id] = fileURL
        } catch {
            downloadError = (error as? APIError)?.errorDescription ?? "Couldn't download that file."
        }
    }
}

// MARK: - Screen

struct TaskDetailView: View {
    let taskId: String
    @EnvironmentObject private var appState: AppState
    @StateObject private var viewModel: TaskDetailViewModel
    @Environment(\.dismiss) private var dismiss
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @FocusState private var answerFocused: Bool
    @State private var showAbandonSheet = false
    @State private var showsProvenance = false

    init(taskId: String) {
        self.taskId = taskId
        _viewModel = StateObject(wrappedValue: TaskDetailViewModel(taskId: taskId))
    }

    var body: some View {
        Group {
            switch viewModel.record {
            case .idle, .loading:
                SaliSkeletonList(rows: 5, showsBar: true, lines: 2)
            case .failed(let message):
                ErrorStateView(message) { Task { await viewModel.loadAll(api: appState.api) } }
            case .loaded(let record):
                detail(for: record)
            }
        }
        .navigationTitle("Task")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { controlsMenu }
        .task {
            viewModel.captureLastSeen()
            await viewModel.loadAll(api: appState.api)
        }
        .onDisappear { viewModel.markSeen() }
        .onChange(of: appState.latestEvent?.id) { _, _ in
            // Event-driven, not time-driven (§40): this is where the user watches a task live, so every
            // task frame is handed to the view model, which decides what it costs and how wide it goes.
            guard let event = appState.latestEvent else { return }
            viewModel.handle(event: event, api: appState.api)
        }
        .onChange(of: appState.replayCaughtUpTick) { _, _ in
            // Turn 5: after a WebSocket reconnect + replay, this screen might be showing state
            // that was true 30 seconds ago. Widest-scope reload — the task might have moved to
            // done/failed while offline, artifacts may have appeared, phases may have advanced.
            viewModel.onReplayCaughtUp(api: appState.api)
        }
        .alert("Couldn't send that", isPresented: answerErrorBinding) {
            Button("OK", role: .cancel) { viewModel.answerError = nil }
        } message: {
            Text(viewModel.answerError ?? "")
        }
        .alert("Couldn't complete that", isPresented: controlErrorBinding) {
            Button("OK", role: .cancel) { viewModel.controlError = nil }
        } message: {
            Text(viewModel.controlError ?? "")
        }
        .alert("Download failed", isPresented: downloadErrorBinding) {
            Button("OK", role: .cancel) { viewModel.downloadError = nil }
        } message: {
            Text(viewModel.downloadError ?? "")
        }
        .sheet(isPresented: $showAbandonSheet) { abandonSheet }
    }

    private var taskObjective: String { viewModel.task?.objective ?? "this task" }

    /// Task-scoped activity: the durable seed with the live ring merged on top, newest first. Computed ONCE
    /// per render in `detail(for:)` and handed down — the health verdict, the away digest and the pulse lane
    /// all read the same list, so they can never tell three different stories.
    private var pulse: [SaliEvent] {
        TaskFeed.merge(seed: viewModel.pulseSeed, live: appState.liveEvents, taskId: taskId)
    }

    private func awayDigest(_ events: [SaliEvent]) -> TaskAwayDigest? {
        guard let since = viewModel.lastSeen, !viewModel.awayDismissed else { return nil }
        return TaskAwayDigest.build(events: events, since: since)
    }

    private var controlErrorBinding: Binding<Bool> {
        Binding(get: { viewModel.controlError != nil }, set: { if !$0 { viewModel.controlError = nil } })
    }
    private var downloadErrorBinding: Binding<Bool> {
        Binding(get: { viewModel.downloadError != nil }, set: { if !$0 { viewModel.downloadError = nil } })
    }
    private var answerErrorBinding: Binding<Bool> {
        Binding(get: { viewModel.answerError != nil }, set: { if !$0 { viewModel.answerError = nil } })
    }

    // MARK: Loaded layout

    @ViewBuilder
    private func detail(for record: TaskRecord) -> some View {
        let events = pulse
        let vitals = TaskVitals.read(record.task, facts: record.facts,
                                     watchdog: TaskFeed.watchdogVerdicts(events)[taskId])
        // The plan's real shape, built once per render from `parent_seq` and handed to both the header and
        // the timeline — the two must never disagree about what the plan is.
        let plan = TaskPlan(steps: record.task.steps, parents: record.facts.parents)

        VStack(spacing: 0) {
            TaskVitalsHeader(task: record.task,
                             vitals: vitals,
                             plan: plan,
                             phase: viewModel.currentPhase,
                             phaseCount: viewModel.phases.count,
                             latest: events.first)

            if vitals.health.needsYou {
                waitingBar
            }

            ScrollView {
                sections(record, vitals: vitals, plan: plan, events: events)
            }
            .refreshable { await viewModel.loadAll(api: appState.api) }
            .scrollDismissesKeyboard(.interactively)

            if vitals.health.needsYou {
                answerComposer
            }
        }
        .background(Theme.Colors.background)
        .toolbar {
            // A guaranteed way out of the keyboard while answering.
            ToolbarItemGroup(placement: .keyboard) {
                Spacer()
                Button("Done") { answerFocused = false }
                    .font(Theme.Typography.body.weight(.semibold))
                    .foregroundStyle(Theme.Colors.accent)
            }
        }
    }

    /// The scrolling body, ranked: what came out of the work, who checked it, then the work itself, then the
    /// evidence. `VStack(spacing: 0)` on purpose — `SectionHeader` owns its own vertical rhythm, so any
    /// spacing here would compound with it and the screen would lose its single ruler.
    private func sections(_ record: TaskRecord, vitals: TaskVitals, plan: TaskPlan,
                          events: [SaliEvent]) -> some View {
        LazyVStack(alignment: .leading, spacing: 0) {
            if let digest = awayDigest(events) {
                awayCard(digest)
                    .padding(.top, Theme.Spacing.l)
            }

            if let supersededBy = record.facts.supersededBy, !supersededBy.isEmpty {
                supersededBanner(supersededBy)
                    .padding(.top, Theme.Spacing.l)
            }

            if !viewModel.phases.isEmpty {
                phaseSection
            }

            outputSection(record.task)
            verificationSection(vitals)
            timelineSection(record, vitals: vitals, plan: plan)
            pulseSection(events)
            provenanceSection(record)
        }
        .padding(.horizontal, Theme.Spacing.listMargin)
        .padding(.bottom, Theme.Spacing.xxl)
    }

    // MARK: Waiting on you

    /// Impossible to miss: a pinned bar directly under the vitals, in the one tone reserved for "you are
    /// wanted", with the composer waiting at the bottom of the screen.
    private var waitingBar: some View {
        HStack(alignment: .top, spacing: Theme.Spacing.s) {
            Image(systemName: "questionmark.circle.fill")
                .foregroundStyle(Theme.Colors.warn)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                Text("Waiting on you")
                    .font(Theme.Typography.subheading)
                    .foregroundStyle(Theme.Colors.primaryText)
                Text(viewModel.pendingQuestion?.question
                     ?? "Sali stopped here and resumes the moment you answer.")
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .lineLimit(4)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, Theme.Spacing.listMargin)
        .padding(.vertical, Theme.Spacing.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.Colors.warn.opacity(0.16))
        .overlay(alignment: .bottom) { hairline }
        .accessibilityElement(children: .combine)
    }

    /// Anchored to the keyboard, so answering is one tap and one sentence — never a hunt for a field.
    @ViewBuilder private var answerComposer: some View {
        VStack(spacing: 0) {
            hairline
            if !viewModel.ambiguousCandidates.isEmpty {
                candidatesStrip
            }
            if appState.role.canControl {
                HStack(alignment: .bottom, spacing: Theme.Spacing.s) {
                    TextField("Answer Sali…", text: $viewModel.answerDraft, axis: .vertical)
                        .focused($answerFocused)
                        .font(Theme.Typography.body)
                        .lineLimit(1...4)
                        .padding(.horizontal, Theme.Spacing.m)
                        .padding(.vertical, Theme.Spacing.s)
                        .background(Theme.Colors.surfaceRaised)
                        .clipShape(RoundedRectangle(cornerRadius: Theme.Radius.l, style: .continuous))
                        .saliHairline(radius: Theme.Radius.l)
                        .accessibilityLabel("Your answer")

                    if viewModel.isSendingAnswer {
                        ProgressView().frame(width: 44, height: 44)
                    } else {
                        Button {
                            let text = viewModel.answerDraft
                            answerFocused = false
                            Task { await viewModel.submitAnswer(text, api: appState.api) }
                        } label: {
                            Image(systemName: "arrow.up.circle.fill")
                                .font(Theme.Typography.title)
                                .foregroundStyle(canSendAnswer ? Theme.Colors.accent
                                                               : Theme.Colors.secondaryText.opacity(0.4))
                                .frame(width: 44, height: 44)
                                .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .disabled(!canSendAnswer)
                        .accessibilityLabel("Send answer")
                    }
                }
                .padding(.horizontal, Theme.Spacing.listMargin)
                .padding(.vertical, Theme.Spacing.s)
            } else {
                Text("Only a controller or owner can answer this.")
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, Theme.Spacing.listMargin)
                    .padding(.vertical, Theme.Spacing.m)
            }
        }
        .background(Theme.Colors.surface)
    }

    private var canSendAnswer: Bool {
        !viewModel.answerDraft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private var candidatesStrip: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
            Text("Did you mean one of these? Tap to refine your reply:")
                .font(Theme.Typography.metadata)
                .foregroundStyle(Theme.Colors.tertiaryText)
            ForEach(viewModel.ambiguousCandidates) { candidate in
                Button {
                    viewModel.seedAmbiguousReply(with: candidate)
                    answerFocused = true
                } label: {
                    Text(candidate.question)
                        .font(Theme.Typography.footnote)
                        .multilineTextAlignment(.leading)
                        .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .foregroundStyle(Theme.Colors.accent)
            }
        }
        .padding(.horizontal, Theme.Spacing.listMargin)
        .padding(.vertical, Theme.Spacing.s)
    }

    // MARK: While you were away

    private func awayCard(_ digest: TaskAwayDigest) -> some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
                Text("While you were away")
                    .font(Theme.Typography.subheading)
                    .foregroundStyle(Theme.Colors.primaryText)
                Spacer(minLength: Theme.Spacing.s)
                // The one site that prefixes its own preposition, so it can't take the "at an…" fragment.
                Text(digest.since.saliIsUnknownTime ? "since an unknown time" : "since \(digest.since.saliRelative)")
                    .font(Theme.Typography.metadata)
                    .foregroundStyle(Theme.Colors.tertiaryText)
            }

            VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                ForEach(digest.lines, id: \.self) { line in
                    HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
                        Circle()
                            .fill(Theme.Colors.accent.opacity(0.55))
                            .frame(width: Theme.Spacing.xs, height: Theme.Spacing.xs)
                            .accessibilityHidden(true)
                        Text(line)
                            .font(Theme.Typography.footnote)
                            .foregroundStyle(Theme.Colors.secondaryText)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }

            Button("Got it") {
                withAnimation(Theme.Motion.honoring(reduceMotion, Theme.Motion.gentle)) {
                    viewModel.awayDismissed = true
                }
            }
            .font(Theme.Typography.footnote.weight(.semibold))
            .foregroundStyle(Theme.Colors.accent)
            .frame(minWidth: 44, minHeight: 44)
            .contentShape(Rectangle())
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .saliCard()
        .accessibilityElement(children: .contain)
    }

    private func supersededBanner(_ supersededBy: String) -> some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            Label("Replaced by a newer task", systemImage: "arrow.triangle.branch")
                .font(Theme.Typography.subheading)
                .foregroundStyle(Theme.Colors.primaryText)
            Text("Sali superseded this intent. The work continues in the task that replaced it.")
                .font(Theme.Typography.footnote)
                .foregroundStyle(Theme.Colors.secondaryText)
                .fixedSize(horizontal: false, vertical: true)
            NavigationLink("Open the task that replaced it") {
                TaskDetailView(taskId: supersededBy)
            }
            .font(Theme.Typography.footnote.weight(.semibold))
            .foregroundStyle(Theme.Colors.accent)
            .frame(minHeight: 44)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .saliCard()
    }

    // MARK: Phase ribbon

    private var phaseSection: some View {
        VStack(alignment: .leading, spacing: 0) {
            SectionHeader("Phases", subtitle: phaseSubtitle)
            TaskPhaseRibbon(phases: viewModel.phases)
            if let summary = viewModel.currentPhase?.summary, !summary.isEmpty {
                Text(summary)
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.top, Theme.Spacing.s)
            }
        }
    }

    private var phaseSubtitle: String? {
        guard let phase = viewModel.currentPhase else { return nil }
        return phase.isActive
            ? "Now in phase \(phase.seq) of \(viewModel.phases.count)"
            : "\(viewModel.phases.count) phase\(viewModel.phases.count == 1 ? "" : "s") recorded — none active"
    }

    // MARK: Output

    private func outputSection(_ task: TaskSummary) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            SectionHeader("Output", subtitle: artifactsSubtitle)

            VStack(alignment: .leading, spacing: Theme.Spacing.m) {
                if let result = task.result, !result.isEmpty {
                    Text(result)
                        .font(Theme.Typography.callout)
                        .foregroundStyle(Theme.Colors.primaryText)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    TaskQuietNote(task.state == .done ? "Finished without a written result."
                                                      : "No result recorded yet.",
                                  detail: task.state == .done
                                      ? "The work is archived even when Sali wrote no summary of it."
                                      : "Sali writes this as the work settles.")
                }
                artifactsContent
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .saliCard()

            if task.result?.isEmpty == false && task.state.isActive {
                SectionFooter("Written while the task is still running — the reviewer, not this text, "
                              + "decides when the work is done.")
            }
        }
    }

    private var artifactsSubtitle: String? {
        guard let list = viewModel.artifacts.value, !list.isEmpty else { return nil }
        return "\(list.count) file\(list.count == 1 ? "" : "s")"
    }

    @ViewBuilder
    private var artifactsContent: some View {
        switch viewModel.artifacts {
        case .idle, .loading:
            SaliSkeletonRow(lines: 1, showsPill: false)
        case .failed(let message):
            HStack {
                Text(message)
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.secondaryText)
                Spacer(minLength: Theme.Spacing.s)
                Button("Try again") { Task { await viewModel.loadArtifacts(api: appState.api) } }
                    .font(Theme.Typography.footnote)
                    .frame(minWidth: 44, minHeight: 44)
                    .contentShape(Rectangle())
            }
        case .loaded(let list) where list.isEmpty:
            TaskQuietNote("No files produced yet.",
                          detail: "Anything this task writes into its workspace shows up here to download.")
        case .loaded(let list):
            VStack(spacing: Theme.Spacing.m) {
                ForEach(list) { artifact in
                    ArtifactRow(
                        artifact: artifact,
                        isDownloading: viewModel.downloadingArtifactId == artifact.id,
                        downloadedURL: viewModel.downloadedFiles[artifact.id]
                    ) {
                        Task { await viewModel.download(artifact, api: appState.api) }
                    }
                }
            }
        }
    }

    // MARK: Verification

    /// Two independent authorities, never merged: the reviewer's verdict on the whole task, and the count of
    /// steps a tool actually confirmed. Neither is inferred from the other.
    private func verificationSection(_ vitals: TaskVitals) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            SectionHeader("Verification", subtitle: verificationSubtitle(vitals))

            VStack(alignment: .leading, spacing: Theme.Spacing.s) {
                if let reviews = viewModel.reviews, reviews.reviewerStatus != nil || reviews.attempts != nil {
                    HStack(spacing: Theme.Spacing.s) {
                        if let status = reviews.reviewerStatus {
                            StatusPill(status.replacingOccurrences(of: "_", with: " ").capitalized,
                                       color: reviewColor(status))
                        }
                        if let attempts = reviews.attempts {
                            Text("\(attempts) review attempt\(attempts == 1 ? "" : "s")")
                                .font(Theme.Typography.metadata)
                                .foregroundStyle(Theme.Colors.tertiaryText)
                        }
                    }
                    if let reason = reviews.latest?.reason, !reason.isEmpty {
                        Text(reason)
                            .font(Theme.Typography.footnote)
                            .foregroundStyle(Theme.Colors.secondaryText)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let history = reviews.reviews, history.count > 1 {
                        DisclosureGroup("Every attempt (\(history.count))") {
                            ForEach(history) { entry in
                                HStack {
                                    Text("Attempt \(entry.sequence ?? 0)")
                                    Spacer(minLength: Theme.Spacing.s)
                                    Text(entry.verdict?.replacingOccurrences(of: "_", with: " ").capitalized
                                         ?? "—")
                                }
                                .font(Theme.Typography.metadata)
                                .foregroundStyle(Theme.Colors.tertiaryText)
                                .frame(minHeight: 44)
                            }
                        }
                        .font(Theme.Typography.footnote)
                        .tint(Theme.Colors.accent)
                    }
                } else {
                    TaskQuietNote("No reviewer verdict recorded yet.",
                                  detail: "The reviewer — not the written result — is what decides this "
                                        + "task is done.")
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .saliCard()
        }
    }

    private func verificationSubtitle(_ vitals: TaskVitals) -> String? {
        guard vitals.stepCount > 0 else { return nil }
        return "\(vitals.verifiedCount) of \(vitals.stepCount) steps confirmed by a tool"
    }

    private func reviewColor(_ status: String) -> Color {
        switch status.uppercased() {
        case "PASS": Theme.Colors.secondaryText
        case "NEEDS_REWORK": Theme.Colors.warn
        case "BLOCKED": Theme.Colors.danger
        default: Theme.Colors.idle
        }
    }

    // MARK: Timeline

    private func timelineSection(_ record: TaskRecord, vitals: TaskVitals, plan: TaskPlan) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            SectionHeader("Timeline", subtitle: timelineSubtitle(vitals, plan: plan))
            if record.task.steps.isEmpty {
                TaskQuietNote("No steps recorded for this task yet.",
                              detail: "A plan appears here the moment Sali writes one.")
            } else {
                TaskTimeline(plan: plan, facts: record.facts)
            }
        }
    }

    private func timelineSubtitle(_ vitals: TaskVitals, plan: TaskPlan) -> String? {
        guard vitals.stepCount > 0 else { return nil }
        var text = "\(vitals.doneCount) of \(vitals.stepCount) done"
        if vitals.failedCount > 0 { text += " · \(vitals.failedCount) failed" }
        // Only ever said when the plan really is nested (migration 0045) — a flat plan says nothing extra.
        if plan.isNested {
            let groups = plan.nodes.filter(\.hasChildren).count
            text += " · \(groups) group\(groups == 1 ? "" : "s")"
        }
        return text
    }

    // MARK: Pulse

    /// Task-scoped, human-level activity — alive during work, quiet when nothing is happening. Never a debug
    /// log: streaming tokens and the agent's own "thinking" frames are filtered out in `TaskFeed`.
    private func pulseSection(_ all: [SaliEvent]) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            SectionHeader("Pulse", subtitle: "What Sali has done on this task recently")
            let events = Array(all.prefix(8))
            if events.isEmpty {
                TaskQuietNote("Quiet — nothing recorded on this task recently.",
                              detail: "This lane fills in as Sali works; it is never a debug log.")
            } else {
                VStack(alignment: .leading, spacing: Theme.Spacing.s) {
                    ForEach(events) { event in
                        HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
                            Text(TaskFeed.summary(for: event))
                                .font(Theme.Typography.footnote)
                                .foregroundStyle(Theme.Colors.secondaryText)
                                .fixedSize(horizontal: false, vertical: true)
                            Spacer(minLength: Theme.Spacing.s)
                            if let timestamp = event.timestamp {
                                Text(timestamp.saliRelative)
                                    .font(Theme.Typography.metadata)
                                    .foregroundStyle(Theme.Colors.tertiaryText)
                                    .layoutPriority(1)
                            }
                        }
                        .accessibilityElement(children: .combine)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .saliCard()
            }
        }
    }

    // MARK: Provenance

    /// ONE drawer for everything that explains where the work came from — workspace, skills, research, host
    /// pressure, and the task's own durable record. These used to be five separate sections competing with
    /// the timeline for the top of the screen.
    private func provenanceSection(_ record: TaskRecord) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            SectionHeader("Provenance", subtitle: provenanceSubtitle)
            DisclosureGroup(isExpanded: $showsProvenance) {
                VStack(alignment: .leading, spacing: Theme.Spacing.l) {
                    workspaceBlock(record.facts)
                    skillsBlock
                    researchBlock
                    resourcesBlock
                    recordBlock(record)
                }
                .padding(.top, Theme.Spacing.m)
            } label: {
                Text(showsProvenance ? "Hide the evidence" : "Where this work came from")
                    .font(Theme.Typography.footnote.weight(.semibold))
                    .foregroundStyle(Theme.Colors.accent)
                    .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                    .contentShape(Rectangle())
            }
            .tint(Theme.Colors.accent)
            .saliCard()
            .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.standard), value: showsProvenance)
        }
    }

    private var provenanceSubtitle: String {
        var parts: [String] = []
        if !viewModel.skills.isEmpty { parts.append("\(viewModel.skills.count) skills") }
        if let research = viewModel.research, !research.research.isEmpty {
            parts.append("\(research.research.count) findings")
        }
        return parts.isEmpty ? "Workspace, skills, research and the durable record"
                             : parts.joined(separator: " · ")
    }

    @ViewBuilder
    private func workspaceBlock(_ facts: TaskFacts) -> some View {
        let root = viewModel.workspace?.workspaceRoot ?? viewModel.task?.workspaceRoot
        let mode = viewModel.workspace?.workspaceMode ?? facts.workspaceMode ?? viewModel.task?.workspaceMode

        VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
            SectionHeader("Workspace", emphasis: .secondary)
            if let root, !root.isEmpty {
                Text(URL(fileURLWithPath: root).lastPathComponent)
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.primaryText)
                Text(root)
                    .font(Theme.Typography.monoSmall)
                    .foregroundStyle(Theme.Colors.tertiaryText)
                    .lineLimit(2)
                    .truncationMode(.middle)
            } else {
                Text("No workspace recorded.")
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.secondaryText)
            }
            if let mode, !mode.isEmpty {
                Text(workspaceModeMeaning(mode))
                    .font(Theme.Typography.metadata)
                    .foregroundStyle(Theme.Colors.tertiaryText)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let roots = viewModel.workspace?.roots, roots.count > 1 {
                Text("\(roots.count) linked locations")
                    .font(Theme.Typography.metadata)
                    .foregroundStyle(Theme.Colors.tertiaryText)
            }
        }
    }

    /// `workspace_mode` is `none | explicit | inherited` (src/sali/tasks/models.py) — it says whether the
    /// write roots were chosen for THIS task or handed down from its parent, which is exactly what a person
    /// needs to know before trusting where a multi-day task is writing.
    private func workspaceModeMeaning(_ mode: String) -> String {
        switch mode.lowercased() {
        case "explicit": "Write access was set for this task"
        case "inherited": "Write access inherited from the task that spawned it"
        case "none": "No write access recorded"
        default: "Workspace mode: \(mode)"
        }
    }

    @ViewBuilder
    private var skillsBlock: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
            SectionHeader("Skills", emphasis: .secondary)
            if viewModel.skills.isEmpty {
                Text("No skills recorded.")
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.secondaryText)
            } else {
                ForEach(viewModel.skills) { skill in
                    HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
                        Text(skill.name)
                            .font(Theme.Typography.footnote)
                            .foregroundStyle(Theme.Colors.primaryText)
                        Spacer(minLength: Theme.Spacing.s)
                        if let score = skill.score {
                            Text("\(Int((score * 100).rounded()))%")
                                .font(Theme.Typography.metadata)
                                .foregroundStyle(Theme.Colors.tertiaryText)
                        }
                    }
                    .accessibilityElement(children: .combine)
                }
            }
        }
    }

    @ViewBuilder
    private var researchBlock: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
            SectionHeader("Research", emphasis: .secondary)
            if let research = viewModel.research,
               !(research.research.isEmpty && research.candidates.isEmpty) {
                Text("\(research.research.count) findings · \(research.candidates.count) learning candidates")
                    .font(Theme.Typography.metadata)
                    .foregroundStyle(Theme.Colors.tertiaryText)
                ForEach(research.research.prefix(3)) { entry in
                    Text(entry.brief)
                        .font(Theme.Typography.footnote)
                        .foregroundStyle(Theme.Colors.secondaryText)
                        .lineLimit(2)
                }
            } else {
                Text("No research recorded.")
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.secondaryText)
            }
        }
    }

    @ViewBuilder
    private var resourcesBlock: some View {
        if let resources = viewModel.resources, let state = resources.state {
            VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                SectionHeader("Host", emphasis: .secondary)
                HStack(spacing: Theme.Spacing.s) {
                    StatusPill(state.capitalized, color: resources.resourceState.color)
                    Text(resources.resourceState.meaning)
                        .font(Theme.Typography.metadata)
                        .foregroundStyle(Theme.Colors.tertiaryText)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }

    /// The durable record itself — the fields that explain how a task survives days and restarts.
    private func recordBlock(_ record: TaskRecord) -> some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
            SectionHeader("Record", emphasis: .secondary)
            factLine("Task id", record.task.id)
            if let created = record.task.createdAt { factLine("Created", created.saliRelative) }
            if let updated = record.task.updatedAt { factLine("Last change", updated.saliRelative) }
            if let beat = record.facts.lastHeartbeat {
                factLine("Last heartbeat", "\(taskAge(Date().timeIntervalSince(beat))) ago")
            }
            if let interrupted = record.facts.interruptedAt {
                factLine("Interrupted", "\(taskAge(Date().timeIntervalSince(interrupted))) ago — not yet recovered")
            }
            factLine("Recoveries used", "\(record.facts.retryCount) of \(record.facts.maxRetries)")
            if let supersededBy = record.facts.supersededBy, !supersededBy.isEmpty {
                factLine("Superseded by", supersededBy)
            }
        }
    }

    private func factLine(_ label: String, _ value: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
            Text(label)
                .font(Theme.Typography.metadata)
                .foregroundStyle(Theme.Colors.tertiaryText)
            Spacer(minLength: Theme.Spacing.s)
            Text(value)
                .font(Theme.Typography.metadata)
                .foregroundStyle(Theme.Colors.secondaryText)
                .lineLimit(1)
                .truncationMode(.middle)
        }
        .accessibilityElement(children: .combine)
    }

    // MARK: Controls

    @ToolbarContentBuilder
    private var controlsMenu: some ToolbarContent {
        if appState.role.canControl, let task = viewModel.task, task.state.isActive {
            ToolbarItem(placement: .primaryAction) {
                Menu {
                    Button {
                        Task { await viewModel.pause(api: appState.api) }
                    } label: {
                        Label("Pause", systemImage: "pause.fill")
                    }
                    .disabled(task.state != .running)

                    Button {
                        Task { await viewModel.resume(api: appState.api) }
                    } label: {
                        Label("Resume", systemImage: "play.fill")
                    }
                    .disabled(task.state != .paused)

                    Divider()

                    // Turn 5: "Cancel task" — the natural label §23 asks for. Same
                    // /abandon endpoint (revoke_intent under the hood since Turn 2), same
                    // durable revocation semantics; the word "abandon" was project jargon
                    // that didn't match Almir's mental model.
                    Button(role: .destructive) {
                        showAbandonSheet = true
                    } label: {
                        Label("Cancel task", systemImage: "xmark.circle")
                    }
                } label: {
                    if viewModel.isPerformingControl {
                        ProgressView().frame(width: 44, height: 44)
                    } else {
                        Image(systemName: "ellipsis.circle")
                            .frame(width: 44, height: 44)
                            .contentShape(Rectangle())
                    }
                }
                .disabled(viewModel.isPerformingControl)
                .accessibilityLabel("Task controls")
            }
        }
    }

    private var abandonSheet: some View {
        NaturalConfirmationSheet(
            title: "Cancel this task",
            explanation: """
            “\(taskObjective)” will be tombstoned as a cancelled intent. Sali records this as a closed \
            chapter — any steps still depending on it are cancelled with it, and it won't auto-resume on \
            its own. You'll still be able to find it under Tasks → Historical, where Revive starts a new \
            task from the same objective whenever you're ready.
            """,
            confirmLabel: "Cancel task",
            isDestructive: true
        ) {
            Task {
                if await viewModel.abandon(api: appState.api) {
                    dismiss()
                }
            }
        }
    }

    private var hairline: some View {
        Rectangle()
            .fill(Theme.Colors.separator)
            .frame(height: Theme.Stroke.hairline)
    }
}

// MARK: - Pinned vitals

/// The one card that answers "is this healthy?" — objective, phase, the step in hand, the plan, and the
/// health verdict — pinned so it stays true while the timeline scrolls beneath it.
///
/// It is also the screen's PROGRESS INSTRUMENT. Three things move here, and all three are driven by real
/// state arriving over the socket: the rail's fill travels as steps settle, the settled count ticks with a
/// numeric transition, and the newest thing Sali actually did is named underneath — which lands the moment
/// the event does, before any refetch has returned.
private struct TaskVitalsHeader: View {
    let task: TaskSummary
    let vitals: TaskVitals
    let plan: TaskPlan
    let phase: TaskPhase?
    let phaseCount: Int
    /// The newest event on this task, from the merged durable + live stream. Nil when nothing has happened.
    let latest: SaliEvent?

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            HStack(alignment: .top, spacing: Theme.Spacing.s) {
                Text(task.objective)
                    .font(Theme.Typography.titleSmall)
                    .foregroundStyle(Theme.Colors.primaryText)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: Theme.Spacing.s)
                StatusPill(vitals.health.label, color: vitals.health.color)
            }

            if let phase {
                Text("Phase \(phase.seq) of \(phaseCount) · \(phase.name)")
                    .font(Theme.Typography.caption)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .lineLimit(1)
            }

            HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
                if vitals.health.isLive {
                    TaskLiveDot().alignmentGuide(.firstTextBaseline) { $0[.bottom] }
                } else {
                    Image(systemName: vitals.health.symbol)
                        .font(Theme.Typography.caption)
                        .foregroundStyle(vitals.health.color)
                        .accessibilityHidden(true)
                }
                Text(vitals.live)
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.primaryText)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }

            // The group the running step belongs to, when the plan is nested. Said here so the header
            // answers "where in the plan" at both altitudes — the group, and the step inside it.
            if let group = vitals.groupTitle, let caption = vitals.groupCaption {
                HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.xs) {
                    Image(systemName: "arrow.turn.down.right")
                        .font(Theme.Typography.metadata)
                        .foregroundStyle(Theme.Colors.tertiaryText)
                        .accessibilityHidden(true)
                    Text("in “\(group)” · \(caption)")
                        .font(Theme.Typography.metadata)
                        .foregroundStyle(Theme.Colors.tertiaryText)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                .accessibilityElement(children: .combine)
            }

            if !task.steps.isEmpty {
                HStack(spacing: Theme.Spacing.m) {
                    TaskStepRail(steps: task.steps)
                    Text("\(vitals.doneCount)/\(vitals.stepCount)")
                        .font(Theme.Typography.numeralSmall)
                        .foregroundStyle(Theme.Colors.secondaryText)
                        .contentTransition(.numericText())
                        .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.standard),
                                   value: vitals.doneCount)
                        .accessibilityHidden(true)
                }
            }

            HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
                if let caption = vitals.stepCaption {
                    Text(caption)
                }
                Spacer(minLength: Theme.Spacing.s)
                if let note = vitals.note {
                    Text(note).layoutPriority(1)
                }
            }
            .font(Theme.Typography.metadata)
            .foregroundStyle(Theme.Colors.tertiaryText)

            latestLine
        }
        .padding(.horizontal, Theme.Spacing.listMargin)
        .padding(.vertical, Theme.Spacing.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.Colors.surface)
        .overlay(alignment: .bottom) {
            Rectangle()
                .fill(Theme.Colors.separator)
                .frame(height: Theme.Stroke.hairline)
        }
        .accessibilityElement(children: .combine)
    }

    /// The last thing that actually happened on this task, named — the one line that lands the INSTANT the
    /// event does, before any refetch has returned. Shown only while the task is live, and only with a real
    /// timestamp beside it: an undated line would read as "now" without being able to prove it.
    @ViewBuilder private var latestLine: some View {
        if vitals.health.isLive, let latest, let timestamp = latest.timestamp {
            HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
                Image(systemName: "waveform")
                    .font(Theme.Typography.metadata)
                    .foregroundStyle(Theme.Colors.tertiaryText)
                    .accessibilityHidden(true)
                Text(TaskFeed.summary(for: latest))
                    .font(Theme.Typography.metadata)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .lineLimit(1)
                Spacer(minLength: Theme.Spacing.s)
                Text(timestamp.saliRelative)
                    .font(Theme.Typography.metadata)
                    .foregroundStyle(Theme.Colors.tertiaryText)
                    .layoutPriority(1)
            }
            .padding(.top, Theme.Spacing.xs)
            .transition(.opacity)
            .id(latest.id)
            .accessibilityElement(children: .combine)
            .accessibilityLabel("Latest: \(TaskFeed.summary(for: latest)), \(timestamp.saliRelative)")
        }
    }
}

// MARK: - Phase ribbon

/// The altitude between "objective" and "step 12 of 31". Phases exist only once started, so this is the road
/// travelled — the active one is ink, the finished ones are quiet, and nothing is drawn for a future that
/// hasn't been planned.
private struct TaskPhaseRibbon: View {
    let phases: [TaskPhase]

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: Theme.Spacing.xs) {
                ForEach(Array(phases.enumerated()), id: \.element.id) { index, phase in
                    if index > 0 {
                        Rectangle()
                            .fill(Theme.Colors.separator)
                            .frame(width: Theme.Spacing.m, height: Theme.Stroke.hairline)
                            .accessibilityHidden(true)
                    }
                    chip(phase)
                }
            }
            .padding(.vertical, Theme.Spacing.xs)
        }
        .accessibilityElement(children: .contain)
    }

    private func chip(_ phase: TaskPhase) -> some View {
        HStack(spacing: Theme.Spacing.xs) {
            Image(systemName: phase.isActive ? "circle.inset.filled" : "checkmark")
                .font(Theme.Typography.metadata)
                .accessibilityHidden(true)
            Text(phase.name)
                .font(Theme.Typography.caption.weight(phase.isActive ? .semibold : .regular))
                .lineLimit(1)
        }
        .padding(.horizontal, Theme.Spacing.m)
        .padding(.vertical, Theme.Spacing.s)
        .foregroundStyle(phase.isActive ? Theme.Colors.onAccent : Theme.Colors.secondaryText)
        .background(phase.isActive ? Theme.Colors.accent : Theme.Colors.surfaceRaised)
        .clipShape(Capsule())
        .overlay(phase.isActive ? nil : Capsule().strokeBorder(Theme.Colors.border,
                                                              lineWidth: Theme.Stroke.hairline))
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Phase \(phase.seq): \(phase.name), \(phase.isActive ? "active" : "done")")
    }
}

// MARK: - Timeline

/// The centrepiece: the plan as a rail — the past collapsed, the present given the whole stage, the future
/// kept quiet — now drawn in the plan's REAL shape (migration 0045's `parent_seq`).
///
/// Three grouping rules, in order:
///
/// 1. **A parent step is a group.** Its sub-steps render in an indented lane beneath it, its meter is built
///    from those children's own statuses, and the lane opens itself wherever the work — or a failure —
///    actually is. An explicit tap always wins over that.
/// 2. **Consecutive settled childless steps fold into ONE node**, exactly as before.
/// 3. **Everything else stands alone**: running, failed, blocked, waiting, or simply not started. A failure
///    can never be hidden inside a "12 steps done" summary, and the step Sali is on is always the loudest
///    thing on screen.
///
/// A plan with no `parent_seq` anywhere produces no groups at all, so it draws exactly as it did before —
/// a flat plan must never start looking broken.
private struct TaskTimeline: View {
    let plan: TaskPlan
    let facts: TaskFacts

    @State private var expandedRuns: Set<Int> = []
    /// A group the user has explicitly opened or closed. Absent = follow the work (see `isExpanded`).
    @State private var groupChoices: [Int: Bool] = [:]
    @State private var showsAllPending = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    /// How many not-yet-started steps are shown before the rest are folded away.
    private static let pendingWindow = 6

    private enum Node: Identifiable {
        case settled(first: Int, nodes: [TaskPlan.Node])
        case group(TaskPlan.Node)
        case step(TaskPlan.Node)

        var id: Int {
            switch self {
            case .settled(let first, _): -first - 1   // never collides with a step's own seq
            case .group(let node): node.id
            case .step(let node): node.id
            }
        }
    }

    var body: some View {
        let visible = visibleNodes
        return VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(visible.enumerated()), id: \.element.id) { index, node in
                TimelineRow(marker: marker(for: node),
                            isLast: index == visible.count - 1 && hiddenPending == 0,
                            markerInset: markerInset(for: node)) {
                    content(for: node)
                }
                // A step landing changes which node this row IS. Without a transition it swaps in silence;
                // with one, the finished work visibly settles into place.
                .transition(.opacity)
            }
            if hiddenPending > 0 {
                showAllButton
            }
        }
        // The timeline redraws when an event-driven refetch lands. The fingerprint is every step's real
        // status, so ONLY a genuine state change animates — a re-render that changed nothing stays still.
        .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.standard), value: fingerprint)
    }

    /// Every step's real status, in order. Never a timestamp — this must not tick on its own.
    private var fingerprint: String {
        plan.nodes.flatMap(\.allSteps)
            .map { "\($0.seq):\($0.status):\($0.attempts):\($0.verified ? 1 : 0)" }
            .joined(separator: "|")
    }

    // MARK: Nodes

    private var nodes: [Node] {
        var result: [Node] = []
        var run: [TaskPlan.Node] = []

        func flush() {
            guard !run.isEmpty else { return }
            if run.count >= 2 {
                result.append(.settled(first: run[0].id, nodes: run))
            } else {
                result.append(contentsOf: run.map { Node.step($0) })
            }
            run = []
        }

        for node in plan.nodes {
            if node.hasChildren {
                // A group is never folded into a settled run: its shape is the information.
                flush()
                result.append(.group(node))
            } else if node.isSettled {
                run.append(node)
            } else {
                flush()
                result.append(.step(node))
            }
        }
        flush()
        return result
    }

    private static func isPending(_ step: TaskStep) -> Bool {
        step.status == "pending" || step.status == "waiting" || step.status == "blocked"
    }

    private var pendingCount: Int {
        plan.nodes.filter { !$0.hasChildren && Self.isPending($0.step) }.count
    }
    private var hiddenPending: Int {
        showsAllPending ? 0 : max(0, pendingCount - Self.pendingWindow)
    }

    private var visibleNodes: [Node] {
        guard hiddenPending > 0 else { return nodes }
        var shown = 0
        return nodes.filter { node in
            guard case .step(let planNode) = node, Self.isPending(planNode.step) else { return true }
            shown += 1
            return shown <= Self.pendingWindow
        }
    }

    private var showAllButton: some View {
        Button {
            withAnimation(Theme.Motion.honoring(reduceMotion, Theme.Motion.standard)) { showsAllPending = true }
        } label: {
            HStack(spacing: Theme.Spacing.s) {
                Image(systemName: "chevron.down")
                    .font(Theme.Typography.metadata)
                Text("Show \(hiddenPending) more planned step\(hiddenPending == 1 ? "" : "s")")
                    .font(Theme.Typography.footnote)
            }
            .foregroundStyle(Theme.Colors.accent)
            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            .padding(.leading, Theme.Spacing.xl + Theme.Spacing.m)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    // MARK: Markers

    /// A settled run opens with a 44pt tap target, a group with its own header line, and the hero inside a
    /// padded card — so all three need the marker dropped to the centre of that first line; a plain node
    /// already starts at its own top edge.
    private func markerInset(for node: Node) -> CGFloat {
        switch node {
        case .settled: Theme.Spacing.s
        case .group: Theme.Spacing.s
        case .step(let planNode): planNode.step.status == "running" ? Theme.Spacing.s : 0
        }
    }

    private func marker(for node: Node) -> TimelineMarker {
        switch node {
        case .settled:
            return TimelineMarker(symbol: "checkmark.circle", color: Theme.Colors.secondaryText)
        case .group(let planNode):
            if planNode.needsAttention {
                return TimelineMarker(symbol: "exclamationmark.circle.fill", color: Theme.Colors.danger)
            }
            if planNode.isLive {
                return TimelineMarker(symbol: "circle.inset.filled", color: Theme.Colors.accent)
            }
            if planNode.isSettled {
                return TimelineMarker(symbol: "checkmark.circle", color: Theme.Colors.secondaryText)
            }
            return TimelineMarker(symbol: "list.bullet.circle", color: Theme.Colors.idle)
        case .step(let planNode):
            return stepMarker(planNode.step)
        }
    }

    private func stepMarker(_ step: TaskStep) -> TimelineMarker {
        if step.status == "running" {
            return TimelineMarker(symbol: "circle.inset.filled", color: Theme.Colors.accent)
        }
        if step.status == "failed" {
            return TimelineMarker(symbol: "xmark.circle.fill", color: Theme.Colors.danger)
        }
        if step.verified {
            return TimelineMarker(symbol: "checkmark.circle.fill", color: Theme.Colors.accent)
        }
        if step.status == "done" || step.status == "skipped" {
            return TimelineMarker(symbol: "checkmark.circle", color: Theme.Colors.secondaryText)
        }
        if step.status == "blocked" || step.status == "waiting" {
            return TimelineMarker(symbol: "pause.circle", color: Theme.Colors.warn)
        }
        return TimelineMarker(symbol: "circle", color: Theme.Colors.idle)
    }

    // MARK: Content

    @ViewBuilder
    private func content(for node: Node) -> some View {
        switch node {
        case .settled(let first, let nodes):
            settledNode(first: first, steps: nodes.map(\.step))
        case .group(let planNode):
            groupNode(planNode)
        case .step(let planNode):
            if planNode.step.status == "running" {
                heroNode(planNode.step)
            } else {
                plainNode(planNode.step)
            }
        }
    }

    /// The collapsed past. Never hides anything that failed — only settled steps are ever folded in here.
    private func settledNode(first: Int, steps: [TaskStep]) -> some View {
        let isExpanded = expandedRuns.contains(first)
        let verified = steps.filter(\.verified).count
        return VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            Button {
                withAnimation(Theme.Motion.honoring(reduceMotion, Theme.Motion.standard)) {
                    if isExpanded { expandedRuns.remove(first) } else { expandedRuns.insert(first) }
                }
            } label: {
                HStack(spacing: Theme.Spacing.s) {
                    Text("\(steps.count) steps done")
                        .font(Theme.Typography.footnote.weight(.medium))
                        .foregroundStyle(Theme.Colors.secondaryText)
                    if verified > 0 {
                        Text("\(verified) verified")
                            .font(Theme.Typography.metadata)
                            .foregroundStyle(Theme.Colors.tertiaryText)
                    }
                    Spacer(minLength: Theme.Spacing.s)
                    Image(systemName: "chevron.down")
                        .font(Theme.Typography.metadata)
                        .foregroundStyle(Theme.Colors.tertiaryText)
                        .rotationEffect(.degrees(isExpanded ? 0 : -90))
                }
                .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityHint(isExpanded ? "Hides these steps" : "Shows these steps")

            if isExpanded {
                VStack(alignment: .leading, spacing: Theme.Spacing.s) {
                    ForEach(steps) { step in
                        HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
                            Image(systemName: step.verified ? "checkmark.seal.fill" : "checkmark")
                                .font(Theme.Typography.metadata)
                                .foregroundStyle(Theme.Colors.tertiaryText)
                                .accessibilityHidden(true)
                            Text(step.description)
                                .font(Theme.Typography.footnote)
                                .foregroundStyle(Theme.Colors.secondaryText)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        .accessibilityElement(children: .combine)
                    }
                }
                .transition(.opacity.combined(with: .move(edge: .top)))
            }
        }
    }

    // MARK: Groups (sub-steps)

    /// Open where the work is. A group opens itself while something inside it is running or has failed and
    /// closes once it settles — until the user says otherwise, and then their choice holds.
    private func isExpanded(_ node: TaskPlan.Node) -> Bool {
        if let choice = groupChoices[node.id] { return choice }
        return node.isLive || node.needsAttention
    }

    /// A parent step and the sub-steps under it. The parent is the sentence; the children are how it is
    /// being done — so the header always states where the group has got to, in its children's own numbers,
    /// and the lane beneath it is indented off the same rail everything else hangs from.
    private func groupNode(_ node: TaskPlan.Node) -> some View {
        let expanded = isExpanded(node)
        return VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            Button {
                withAnimation(Theme.Motion.honoring(reduceMotion, Theme.Motion.standard)) {
                    groupChoices[node.id] = !expanded
                }
            } label: {
                VStack(alignment: .leading, spacing: Theme.Spacing.s) {
                    HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
                        Text(node.step.description)
                            .font(Theme.Typography.footnote.weight(.medium))
                            .foregroundStyle(node.isSettled ? Theme.Colors.secondaryText
                                                            : Theme.Colors.primaryText)
                            .multilineTextAlignment(.leading)
                            .fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: Theme.Spacing.s)
                        Text("\(node.doneChildren)/\(node.children.count)")
                            .font(Theme.Typography.numeralSmall)
                            .foregroundStyle(Theme.Colors.tertiaryText)
                            .contentTransition(.numericText())
                        Image(systemName: "chevron.down")
                            .font(Theme.Typography.metadata)
                            .foregroundStyle(Theme.Colors.tertiaryText)
                            .rotationEffect(.degrees(expanded ? 0 : -90))
                            .accessibilityHidden(true)
                    }
                    // The same rail the header uses, one level down: a group's progress is nothing more
                    // than its own children's statuses.
                    TaskStepRail(steps: node.children)
                }
                .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityElement(children: .combine)
            .accessibilityLabel(groupLabel(node))
            .accessibilityHint(expanded ? "Hides the sub-steps" : "Shows the sub-steps")

            if expanded {
                VStack(alignment: .leading, spacing: Theme.Spacing.m) {
                    ForEach(node.children) { child in
                        substepRow(child)
                    }
                }
                .padding(.leading, Theme.Spacing.m)
                .overlay(alignment: .leading) {
                    // The lane's own hairline — the sub-steps belong to the step above them, and the eye
                    // should be able to see exactly where the group starts and stops.
                    Rectangle()
                        .fill(Theme.Colors.separator)
                        .frame(width: Theme.Stroke.hairline)
                        .accessibilityHidden(true)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .transition(.opacity.combined(with: .move(edge: .top)))
            } else if let running = node.runningChild {
                // Collapsed by choice while something is happening inside: say WHAT, rather than hiding it.
                HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
                    TaskLiveDot(size: Theme.Spacing.xs + 2)
                        .alignmentGuide(.firstTextBaseline) { $0[.bottom] }
                    Text(running.description)
                        .font(Theme.Typography.metadata)
                        .foregroundStyle(Theme.Colors.secondaryText)
                        .lineLimit(1)
                }
                .accessibilityElement(children: .combine)
            } else if let failed = node.children.first(where: { $0.status == "failed" }) {
                // Collapsed by choice AND a sub-step has failed: a failure must never be hidden behind a
                // collapse. Surface WHICH child failed and its error, right in the collapsed summary.
                HStack(alignment: .top, spacing: Theme.Spacing.s) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .font(Theme.Typography.metadata)
                        .foregroundStyle(Theme.Colors.danger)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(failed.description)
                            .font(Theme.Typography.metadata)
                            .foregroundStyle(Theme.Colors.secondaryText)
                            .lineLimit(1)
                        if let err = failed.lastError, !err.isEmpty {
                            Text(err)
                                .font(Theme.Typography.metadata)
                                .foregroundStyle(Theme.Colors.danger)
                                .lineLimit(2)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
                .accessibilityElement(children: .combine)
            }
        }
    }

    private func groupLabel(_ node: TaskPlan.Node) -> String {
        var text = "\(node.step.description). \(node.doneChildren) of \(node.children.count) sub-steps done"
        if node.failedChildren > 0 { text += ", \(node.failedChildren) failed" }
        if let running = node.runningChild { text += ". Running now: \(running.description)" }
        return text
    }

    /// One sub-step. The running one is the only thing in the lane that gets a surface and a live edge —
    /// everything else is a line of text with a marker, so a four-step group stays four lines tall.
    @ViewBuilder
    private func substepRow(_ step: TaskStep) -> some View {
        let isRunning = step.status == "running"
        let body = HStack(alignment: .top, spacing: Theme.Spacing.s) {
            // A fixed marker column, so every sub-step's text starts on one line no matter which glyph
            // it carries; the height matches a `footnote` line box so the glyph reads as centred on it.
            substepMarker(step)
                .frame(width: Theme.Spacing.m, height: Theme.Spacing.l)
                .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                Text(step.description)
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(substepInk(step))
                    .fixedSize(horizontal: false, vertical: true)

                if let note = step.note, !note.isEmpty {
                    Text(note)
                        .font(Theme.Typography.metadata)
                        .foregroundStyle(Theme.Colors.tertiaryText)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let error = step.lastError, !error.isEmpty {
                    Text(error)
                        .font(Theme.Typography.metadata)
                        .foregroundStyle(Theme.Colors.danger)
                        .lineLimit(3)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let checkpoint = facts.checkpoints[step.seq] {
                    Text("Checkpoint · \(checkpoint)")
                        .font(Theme.Typography.metadata)
                        .foregroundStyle(Theme.Colors.tertiaryText)
                }
                if !badges(step).isEmpty {
                    Text(badges(step).joined(separator: " · "))
                        .font(Theme.Typography.metadata)
                        .foregroundStyle(step.status == "failed" ? Theme.Colors.warn
                                                                 : Theme.Colors.tertiaryText)
                }
            }
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)

        if isRunning {
            // The one sub-step in flight lifts onto its own surface and takes the ambient edge. The
            // negative LEADING inset is what keeps its text on the same line as every other sub-step's —
            // the card grows outward around the row instead of shunting it right — while its trailing
            // edge still lines up with the lane.
            TaskLivePulse(radius: Theme.Radius.s) {
                body
                    .padding(.horizontal, Theme.Spacing.s)
                    .padding(.vertical, Theme.Spacing.s)
                    .background(Theme.Colors.surfaceRaised)
                    .clipShape(RoundedRectangle(cornerRadius: Theme.Radius.s, style: .continuous))
            }
            .padding(.leading, -Theme.Spacing.s)
        } else {
            body
        }
    }

    private func substepInk(_ step: TaskStep) -> Color {
        switch step.status {
        case "running", "failed": Theme.Colors.primaryText
        case "done", "skipped": Theme.Colors.secondaryText
        default: Theme.Colors.secondaryText
        }
    }

    @ViewBuilder
    private func substepMarker(_ step: TaskStep) -> some View {
        switch step.status {
        case "running":
            TaskLiveDot(size: Theme.Spacing.s)
        case "failed":
            Image(systemName: "xmark")
                .font(Theme.Typography.metadata)
                .foregroundStyle(Theme.Colors.danger)
        case "done", "skipped":
            Image(systemName: step.verified ? "checkmark.seal.fill" : "checkmark")
                .font(Theme.Typography.metadata)
                .foregroundStyle(Theme.Colors.tertiaryText)
        case "blocked", "waiting":
            Image(systemName: "pause")
                .font(Theme.Typography.metadata)
                .foregroundStyle(Theme.Colors.warn)
        default:
            Circle()
                .strokeBorder(Theme.Colors.borderStrong, lineWidth: Theme.Stroke.hairline)
                .frame(width: Theme.Spacing.s - 1, height: Theme.Spacing.s - 1)
        }
    }

    /// The HERO — the step actually in flight. This is what the owner watches, so it is the only thing on
    /// the screen that gets a raised card, a title-weight line, a live mark AND the one ambient edge in the
    /// feature, breathing on `Motion.beat`.
    private func heroNode(_ step: TaskStep) -> some View {
        TaskLivePulse {
            VStack(alignment: .leading, spacing: Theme.Spacing.s) {
                HStack(spacing: Theme.Spacing.s) {
                    TaskLiveDot()
                    Text("Running now")
                        .font(Theme.Typography.metadata)
                        .foregroundStyle(Theme.Colors.secondaryText)
                    Spacer(minLength: Theme.Spacing.s)
                    Text("Step \(step.seq)")
                        .font(Theme.Typography.metadata)
                        .foregroundStyle(Theme.Colors.tertiaryText)
                }

                Text(step.description)
                    .font(Theme.Typography.titleSmall)
                    .foregroundStyle(Theme.Colors.primaryText)
                    .fixedSize(horizontal: false, vertical: true)

                // The step's CONTRACT (backend migration 0055). "Done when" is what completion
                // concretely means for this one step — the engine verifies it before accepting the
                // mark, so it is also the bar the user can hold the work to. "Won't touch" is what
                // this step deliberately leaves for a later step (the one-step-at-a-time guarantee).
                if let dod = step.definitionOfDone, !dod.isEmpty {
                    Label("Done when: \(dod)", systemImage: "checkmark.seal")
                        .font(Theme.Typography.footnote)
                        .foregroundStyle(Theme.Colors.secondaryText)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let excl = step.scopeExcludes, !excl.isEmpty {
                    Label("Won't touch: \(excl)", systemImage: "hand.raised")
                        .font(Theme.Typography.metadata)
                        .foregroundStyle(Theme.Colors.tertiaryText)
                        .fixedSize(horizontal: false, vertical: true)
                }

                if let note = step.note, !note.isEmpty {
                    Text(note)
                        .font(Theme.Typography.footnote)
                        .foregroundStyle(Theme.Colors.secondaryText)
                        .fixedSize(horizontal: false, vertical: true)
                }

                // `checkpoint` (TaskStepResponse) — the in-step progress bag that lets a resumed step
                // continue instead of starting over. It is the difference between "day three of this step"
                // and "day three, restarted twice".
                if let checkpoint = facts.checkpoints[step.seq] {
                    Label("Resuming mid-step · \(checkpoint)", systemImage: "bookmark.fill")
                        .font(Theme.Typography.metadata)
                        .foregroundStyle(Theme.Colors.tertiaryText)
                        .fixedSize(horizontal: false, vertical: true)
                }

                if step.attempts > 1 {
                    Text("Attempt \(step.attempts)")
                        .font(Theme.Typography.metadata)
                        .foregroundStyle(Theme.Colors.warn)
                }

                if let error = step.lastError, !error.isEmpty {
                    Text(error)
                        .font(Theme.Typography.footnote)
                        .foregroundStyle(Theme.Colors.danger)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .saliCard()
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Running now, step \(step.seq): \(step.description)")
    }

    /// Everything else: failed, blocked, waiting, or simply not started yet. Weight and ink carry the
    /// difference — a pending step is quiet, a failed one states its error.
    private func plainNode(_ step: TaskStep) -> some View {
        let isPending = Self.isPending(step)
        return VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
            Text(step.description)
                .font(Theme.Typography.footnote)
                .foregroundStyle(isPending ? Theme.Colors.secondaryText : Theme.Colors.primaryText)
                .fixedSize(horizontal: false, vertical: true)

            if let note = step.note, !note.isEmpty {
                Text(note)
                    .font(Theme.Typography.metadata)
                    .foregroundStyle(Theme.Colors.tertiaryText)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if let error = step.lastError, !error.isEmpty {
                Text(error)
                    .font(Theme.Typography.metadata)
                    .foregroundStyle(Theme.Colors.danger)
                    .lineLimit(3)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if let checkpoint = facts.checkpoints[step.seq] {
                Text("Checkpoint · \(checkpoint)")
                    .font(Theme.Typography.metadata)
                    .foregroundStyle(Theme.Colors.tertiaryText)
            }

            if !badges(step).isEmpty {
                Text(badges(step).joined(separator: " · "))
                    .font(Theme.Typography.metadata)
                    .foregroundStyle(step.status == "failed" ? Theme.Colors.warn : Theme.Colors.tertiaryText)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.top, Theme.Spacing.xs)
        .accessibilityElement(children: .combine)
    }

    private func badges(_ step: TaskStep) -> [String] {
        var badges: [String] = []
        if step.attempts > 1 { badges.append("Attempt \(step.attempts)") }
        if let failureClass = step.failureClass, !failureClass.isEmpty {
            badges.append(failureClass.replacingOccurrences(of: "_", with: " "))
        }
        if step.verified { badges.append("Verified") }
        if step.status == "blocked" { badges.append("Waiting on a prerequisite") }
        return badges
    }
}

private struct TimelineMarker {
    let symbol: String
    let color: Color
}

/// One rail row: a fixed marker column and the content beside it, with the connector drawn as the row's own
/// background so it spans exactly this row's height (including the gap below it) — no greedy layout, no
/// guessing at heights.
private struct TimelineRow<Content: View>: View {
    private let marker: TimelineMarker
    private let isLast: Bool
    /// Drops the marker to the vertical centre of the row's FIRST line, for content that doesn't start at
    /// its own top edge — a 44pt tap target, or a card with its own padding. Every node on the rail still
    /// shares one centre line horizontally; this only fixes the vertical.
    private let markerInset: CGFloat
    private let content: Content

    /// The marker column: one `Spacing.xl` square, so every node on the rail shares one centre line.
    private static var railWidth: CGFloat { Theme.Spacing.xl }

    init(marker: TimelineMarker, isLast: Bool, markerInset: CGFloat = 0,
         @ViewBuilder content: () -> Content) {
        self.marker = marker
        self.isLast = isLast
        self.markerInset = markerInset
        self.content = content()
    }

    var body: some View {
        HStack(alignment: .top, spacing: Theme.Spacing.m) {
            Image(systemName: marker.symbol)
                .font(Theme.Typography.footnote)
                .foregroundStyle(marker.color)
                .frame(width: Self.railWidth, height: Self.railWidth)
                .padding(.top, markerInset)
                .accessibilityHidden(true)

            content
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.bottom, isLast ? 0 : Theme.Spacing.l)
        .background(alignment: .topLeading) {
            // The connector is the row's own background, so it spans exactly this row's height — including
            // the gap below it — without any greedy layout or a guess at the next row's position.
            if !isLast {
                Rectangle()
                    .fill(Theme.Colors.separator)
                    .frame(width: Theme.Stroke.hairline)
                    .padding(.leading, (Self.railWidth - Theme.Stroke.hairline) / 2)
                    .padding(.top, Self.railWidth + markerInset)
            }
        }
    }
}

// MARK: - Artifact row

private struct ArtifactRow: View {
    let artifact: ArtifactMeta
    let isDownloading: Bool
    let downloadedURL: URL?
    let onDownload: () -> Void

    var body: some View {
        HStack(spacing: Theme.Spacing.m) {
            Image(systemName: icon)
                .foregroundStyle(Theme.Colors.secondaryText)
                .frame(width: Theme.Spacing.xl)
                .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                Text(artifact.filename)
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.primaryText)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Text(detailLine)
                    .font(Theme.Typography.metadata)
                    .foregroundStyle(Theme.Colors.tertiaryText)
            }

            Spacer(minLength: Theme.Spacing.s)

            trailing
        }
        .accessibilityElement(children: .combine)
    }

    private var detailLine: String {
        var parts: [String] = []
        if let size = artifact.size {
            parts.append(ByteCountFormatter.string(fromByteCount: Int64(size), countStyle: .file))
        }
        parts.append(artifact.artifactType.capitalized)
        if !artifact.available { parts.append("Unavailable") }
        return parts.joined(separator: " · ")
    }

    @ViewBuilder
    private var trailing: some View {
        // The glyphs stay their natural size; the hit areas are a full 44pt (item 9).
        if let downloadedURL {
            ShareLink(item: downloadedURL) {
                Image(systemName: "square.and.arrow.up")
                    .frame(width: 44, height: 44)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .foregroundStyle(Theme.Colors.accent)
            .accessibilityLabel("Share \(artifact.filename)")
        } else if isDownloading {
            ProgressView().frame(width: 44, height: 44)
        } else {
            Button(action: onDownload) {
                Image(systemName: "arrow.down.circle")
                    .frame(width: 44, height: 44)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .foregroundStyle(Theme.Colors.accent)
            .disabled(!artifact.available)
            .accessibilityLabel("Download \(artifact.filename)")
        }
    }

    private var icon: String {
        if artifact.contentType.hasPrefix("image/") { return "photo" }
        if artifact.contentType.hasPrefix("video/") { return "video" }
        if artifact.contentType == "application/pdf" { return "doc.richtext" }
        if artifact.contentType.hasPrefix("text/") { return "doc.text" }
        return "doc"
    }
}

// MARK: - Resource level color (Task Detail only — the authoritative color/meaning still lives on the model)

private extension ResourceLevel {
    var color: Color {
        switch self {
        case .safe: Theme.Colors.secondaryText
        case .elevated: Theme.Colors.accent
        case .high: Theme.Colors.warn
        case .critical, .emergency: Theme.Colors.danger
        }
    }
}

#Preview {
    NavigationStack {
        TaskDetailView(taskId: "preview-task")
    }
    .environmentObject(AppState())
}
