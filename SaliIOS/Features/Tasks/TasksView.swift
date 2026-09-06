import SwiftUI

// Task management (Prompt 13, §14/§15) — a tab root, so this owns its own `NavigationStack`. Active tasks
// come from `GET /api/v1/tasks` (which never includes abandoned tasks server-side); a separate "Historical /
// abandoned" section is sourced from `GET /api/v1/intent/revoked` and is never presented as active (§15).
// Finished work would otherwise VANISH — the DB rows are deleted on completion — so the "History" segment
// reads the durable sali-works archive via `GET /api/v1/tasks/history` (§15: work Sali did is never lost).
//
// THE SCREEN IS TRIAGE, NOT A LIST. Sali now drives multi-day coding tasks forward on its own, so the two
// questions this screen has to answer at a glance are "is anything parked on me?" and "what is each task
// doing right now?" — hence: a FIXED control bar (filter + state of play) that never scrolls away, a
// "Needs you" group pinned above everything else, and a row built around the live line + a segmented step
// rail rather than a title and a status pill.
//
// This file also owns the task-truth layer the whole feature shares — `TaskRecord`, `TaskFacts`,
// `TaskHealth`, `TaskVitals`, the step rail — because `TaskDetailView` must answer those same questions
// with exactly the same rules. Every value in it comes from a real backend field; nothing is inferred from
// the presence of text (§: the reviewer, not the app, decides when work is done).

/// One tombstoned intent, as returned by `GET /api/v1/intent/revoked`.
struct RevokedTask: Decodable, Identifiable, Sendable {
    var id: String { taskId }
    let taskId: String
    let objective: String
    let reason: String?
    let revokedAt: Date?
    let supersededBy: String?

    enum CodingKeys: String, CodingKey {
        case taskId = "task_id", objective, reason, revokedAt = "revoked_at", supersededBy = "superseded_by"
    }
}

private struct RevokedTasksResponse: Decodable, Sendable { let revoked: [RevokedTask] }

/// Lenient timestamp reading for everything this feature decodes by hand.
///
/// `APIClient.decoder`'s date strategy THROWS on a stamp it can't read, and one throw fails the WHOLE
/// response — which is exactly how the History tab came to render empty: a single archived row carried a
/// Postgres-style stamp ("2026-09-02 22:00:34.833226+00:00", with a SPACE instead of the ISO 'T') and took
/// every other row down with it. The server normalises that now, but the client must not depend on it:
/// anything here that can degrade reads its timestamps as STRINGS and comes through this function, where
/// an unreadable value means "no date" for that one field and nothing else.
enum TaskTime {
    static func parse(_ raw: String?) -> Date? {
        guard let raw else { return nil }
        var text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        // "2026-09-02 22:00:34…" → "2026-09-02T22:00:34…". One character, and it used to cost the screen.
        if text.count > 10, text.dropFirst(10).hasPrefix(" ") {
            let index = text.index(text.startIndex, offsetBy: 10)
            text.replaceSubrange(index...index, with: "T")
        }
        // The shared lenient parser already covers ISO-8601 with and without fractional seconds, and
        // Python's timezone-naive `datetime.isoformat()`.
        if let date = SaliISO8601.parse(text) { return date }
        // Date-only ("2026-09-02"), the one remaining shape a `meta.json` written by hand can carry.
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "UTC")
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter.date(from: text)
    }
}

/// One finished task from the durable archive, as returned by `GET /api/v1/tasks/history` (a top-level
/// JSON array). These rows outlive the database — the task record is deleted the moment work completes —
/// so a history entry is NEVER fetchable via `GET /tasks/{id}` and must never link to `TaskDetailView`.
/// `status` is one of done / failed / abandoned / cancelled / superseded.
///
/// DECODED DEFENSIVELY, ON PURPOSE. The archive is a directory of `meta.json` files written by several
/// generations of the backend, so this initializer has exactly ONE failure mode — the element isn't a JSON
/// object at all — and every field inside it degrades instead of throwing. A row the app can only partly
/// read is still a row the user gets to see.
struct TaskHistoryEntry: Decodable, Identifiable, Sendable {
    let id: String
    let objective: String
    let status: String
    let result: String?
    let createdAt: Date?
    let updatedAt: Date?
    let workspaceRoot: String?
    /// The server sent a stamp this build couldn't read. Said out loud on the row ("time not recorded")
    /// rather than quietly rendering as undated — a degraded field is still a fact about the data.
    let hasUnreadableStamp: Bool

    private enum CodingKeys: String, CodingKey {
        case id, objective, status, result
        case createdAt = "created_at", updatedAt = "updated_at", workspaceRoot = "workspace_root"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        func text(_ key: CodingKeys) -> String? {
            guard let value = (try? container.decodeIfPresent(String.self, forKey: key)) ?? nil else {
                return nil
            }
            let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
            return trimmed.isEmpty ? nil : trimmed
        }

        let rawCreated = text(.createdAt)
        let rawUpdated = text(.updatedAt)
        objective = text(.objective) ?? "Untitled work"
        status = text(.status) ?? ""
        result = text(.result)
        workspaceRoot = text(.workspaceRoot)
        createdAt = TaskTime.parse(rawCreated)
        updatedAt = TaskTime.parse(rawUpdated)
        hasUnreadableStamp = (rawCreated != nil && createdAt == nil)
            || (rawUpdated != nil && updatedAt == nil)
        // A row with no id is still a row. Keyed by what it actually is, so `ForEach` identity survives a
        // refresh — never a fresh UUID, which would re-insert every row on every render.
        id = text(.id) ?? "archive|\(rawUpdated ?? rawCreated ?? "undated")|\(objective)"
    }

    var state: TaskState { TaskState(rawValue: status) ?? .other }
    /// The real backend task id, or nil for an archive row the server sent WITHOUT one — its `id` is then
    /// a synthesized key that is fine for ForEach identity but is not addressable, so nothing may be
    /// deleted by it. Delete is offered only when this is non-nil.
    var taskId: String? { id.hasPrefix("archive|") ? nil : id }
    /// The outcome word for the pill. An archive row with no status is unusual and says so, rather than
    /// borrowing "Done" from the section it happens to sit in.
    var outcomeLabel: String { status.isEmpty ? "Not recorded" : state.label }
    /// When the work actually stopped. The archive always stamps `updated_at`; `created_at` is the honest
    /// fallback rather than inventing "now" for a row the server couldn't date.
    var finishedAt: Date? { updatedAt ?? createdAt }
    /// How long the work took, only when BOTH ends are known and ordered. Never estimated.
    var duration: TimeInterval? {
        guard let createdAt, let updatedAt, updatedAt > createdAt else { return nil }
        return updatedAt.timeIntervalSince(createdAt)
    }
}

/// The history array itself, decoded row by row. `TaskHistoryEntry` already degrades every field, so this
/// is the last line of defence: an element with a shape this build can't read at all is skipped and
/// counted, never allowed to blank the screen behind it.
struct TaskHistoryFeed: Decodable, Sendable {
    let entries: [TaskHistoryEntry]
    let skipped: Int

    init(from decoder: Decoder) throws {
        var container = try decoder.unkeyedContainer()
        var entries: [TaskHistoryEntry] = []
        var skipped = 0
        while !container.isAtEnd {
            if let entry = try? container.decode(TaskHistoryEntry.self) {
                entries.append(entry)
            } else if (try? container.decode(JSONValue.self)) != nil {
                // Consumed as an opaque value so the cursor advances past the unreadable row.
                skipped += 1
            } else {
                break
            }
        }
        self.entries = entries
        self.skipped = skipped
    }
}

/// One day of finished work — the shape the History tab actually reads in. Built from real `finished_at`
/// stamps; rows the archive couldn't date are collected in their own group instead of being guessed into
/// today.
struct TaskHistoryDay: Identifiable, Sendable {
    let id: String
    let title: String
    let detail: String
    let entries: [TaskHistoryEntry]

    /// Newest day first, newest row first inside each day.
    static func group(_ entries: [TaskHistoryEntry],
                      calendar: Calendar = .current, now: Date = Date()) -> [TaskHistoryDay] {
        let sorted = entries.sorted { ($0.finishedAt ?? .distantPast) > ($1.finishedAt ?? .distantPast) }
        var order: [String] = []
        var buckets: [String: [TaskHistoryEntry]] = [:]
        var days: [String: Date] = [:]

        for entry in sorted {
            let key: String
            if let date = entry.finishedAt {
                let day = calendar.startOfDay(for: date)
                key = String(day.timeIntervalSince1970)
                days[key] = day
            } else {
                key = "undated"
            }
            if buckets[key] == nil { order.append(key) }
            buckets[key, default: []].append(entry)
        }

        return order.map { key in
            let rows = buckets[key] ?? []
            let failed = rows.filter { $0.state == .failed }.count
            var detail = "\(rows.count) finished"
            if failed > 0 { detail += " · \(failed) failed" }
            return TaskHistoryDay(
                id: key,
                title: days[key].map { title(for: $0, calendar: calendar, now: now) } ?? "Date not recorded",
                detail: detail,
                entries: rows
            )
        }
    }

    private static func title(for day: Date, calendar: Calendar, now: Date) -> String {
        if day.saliIsUnknownTime { return "Undated" }
        if calendar.isDateInToday(day) { return "Today" }
        if calendar.isDateInYesterday(day) { return "Yesterday" }
        if calendar.isDate(day, equalTo: now, toGranularity: .year) {
            return day.formatted(.dateTime.weekday(.wide).day().month(.wide))
        }
        return day.formatted(.dateTime.day().month(.wide).year())
    }
}

/// Semantic color for a task's lifecycle state — shared across Activity, Tasks, and Task Detail.
extension TaskState {
    /// Waiting / blocked genuinely want the user, and failed genuinely wants attention. A task that is
    /// simply running, or that finished successfully, is the normal case and reads as ink (item 12) —
    /// otherwise a healthy Tasks tab is a wall of green and blue that means nothing.
    var color: Color {
        switch self {
        case .open, .running: Theme.Colors.accent
        case .paused, .waiting, .waitingForUser, .blocked,
             .needsChanges, .blockedByReview: Theme.Colors.warn   // Turn 8
        case .done: Theme.Colors.secondaryText
        case .failed: Theme.Colors.danger
        case .abandoned, .cancelled, .superseded, .other: Theme.Colors.idle
        }
    }
}

// MARK: - The task-truth layer

/// The `TaskResponse` fields the app used to throw away.
///
/// `TaskSummary` (Models/DomainModels) carries the objective/status/steps and nothing about *durability*.
/// The backend has sent these five for a long time and no screen ever showed them, which mattered less when
/// a task lived for minutes. It matters now: a task runs for days, across daemon restarts, and the honest
/// answer to "is this healthy?" is written in exactly these fields.
///
/// - `interrupted_at` — stamped by `tasks/recovery.py::mark_task_interrupted` when a running task's process
///   died, and by `store.py` when a task is suspended for a restart. Cleared on successful recovery.
/// - `retry_count` / `max_retries` — how many times this task has been recovered and resumed, and the
///   ceiling after which it will not be.
/// - `last_heartbeat` — the process-liveness stamp the backend's own watchdog measures against
///   (`TaskWatchdog.heartbeat_timeout`, 5 minutes, after which it classifies a task `orphaned`).
/// - `superseded_by` — the task that replaced this one.
/// - `workspace_mode` — none | explicit | inherited: whether the write roots were chosen for this task or
///   inherited from its parent.
///
/// Plus one from `TaskStepResponse`: `checkpoint`, a step's mid-flight progress bag, which is what lets a
/// resumed step pick up where it stopped rather than restarting.
struct TaskFacts: Sendable, Equatable {
    var interruptedAt: Date?
    var lastHeartbeat: Date?
    var supersededBy: String?
    var workspaceMode: String?
    var retryCount: Int = 0
    var maxRetries: Int = 3
    /// step seq → a one-line, human rendering of that step's `checkpoint` bag.
    var checkpoints: [Int: String] = [:]
    /// step seq → the seq of its PARENT step (migration 0045's `parent_seq`). A seq that isn't a key here
    /// is a top-level step. Empty for a flat plan, which is still the common case.
    var parents: [Int: Int] = [:]

    static let unknown = TaskFacts()

    /// A checkpoint is an arbitrary bag; render at most three keys, and only ones that flatten to a scalar.
    /// Never invent a shape for it.
    static func checkpointLine(_ bag: [String: JSONValue]) -> String? {
        let parts = bag.sorted { $0.key < $1.key }.prefix(3).compactMap { key, value -> String? in
            guard let text = value.taskPlainText else { return nil }
            return "\(key.replacingOccurrences(of: "_", with: " ")) \(text)"
        }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }
}

private extension JSONValue {
    /// Scalars only. A nested object or array in a checkpoint has no honest one-line form, so it is skipped
    /// rather than rendered as machine text.
    var taskPlainText: String? {
        switch self {
        case .string(let value): return value.isEmpty ? nil : value
        case .bool(let value): return value ? "yes" : "no"
        case .number(let value):
            if value == value.rounded() && abs(value) < 1e12 { return String(Int(value)) }
            return String(format: "%.2f", value)
        case .null, .array, .object: return nil
        }
    }
}

/// `GET /api/v1/tasks/{id}` and each element of `GET /api/v1/tasks` — the shared `TaskSummary` PLUS the
/// durability fields it doesn't model. Decoded from the same JSON object in one pass, so surfacing them
/// costs no extra request.
struct TaskRecord: Decodable, Identifiable, Sendable {
    let task: TaskSummary
    let facts: TaskFacts
    var id: String { task.id }

    private enum Keys: String, CodingKey {
        case steps
        case interruptedAt = "interrupted_at"
        case lastHeartbeat = "last_heartbeat"
        case supersededBy = "superseded_by"
        case workspaceMode = "workspace_mode"
        case retryCount = "retry_count"
        case maxRetries = "max_retries"
    }

    /// Only `seq`, `checkpoint` and `parent_seq`; the rest of the step is already decoded by `TaskSummary`.
    /// `parent_seq` (migration 0045) is the whole of the plan's hierarchy: one flat, ordered `seq` space
    /// where a step names its parent, so nothing else about the step model had to change.
    private struct StepExtras: Decodable {
        let seq: Int
        let checkpoint: [String: JSONValue]?
        let parentSeq: Int?

        private enum CodingKeys: String, CodingKey { case seq, checkpoint, parentSeq = "parent_seq" }

        init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            seq = try container.decode(Int.self, forKey: .seq)
            checkpoint = (try? container.decodeIfPresent([String: JSONValue].self, forKey: .checkpoint)) ?? nil
            parentSeq = (try? container.decodeIfPresent(Int.self, forKey: .parentSeq)) ?? nil
        }
    }

    init(from decoder: Decoder) throws {
        task = try TaskSummary(from: decoder)
        let container = try decoder.container(keyedBy: Keys.self)

        var checkpoints: [Int: String] = [:]
        var parents: [Int: Int] = [:]
        if let extras = try? container.decode([StepExtras].self, forKey: .steps) {
            for extra in extras {
                if let bag = extra.checkpoint, let line = TaskFacts.checkpointLine(bag) {
                    checkpoints[extra.seq] = line
                }
                // A step that claims itself as its own parent is not a hierarchy; it is a cycle.
                if let parent = extra.parentSeq, parent != extra.seq { parents[extra.seq] = parent }
            }
        }

        facts = TaskFacts(
            interruptedAt: Self.optional(container, .interruptedAt, Date.self),
            lastHeartbeat: Self.optional(container, .lastHeartbeat, Date.self),
            supersededBy: Self.optional(container, .supersededBy, String.self),
            workspaceMode: Self.optional(container, .workspaceMode, String.self),
            retryCount: Self.optional(container, .retryCount, Int.self) ?? 0,
            maxRetries: Self.optional(container, .maxRetries, Int.self) ?? 3,
            checkpoints: checkpoints,
            parents: parents
        )
    }

    /// A field the server didn't send, or sent in a shape this build doesn't know, is "not recorded" —
    /// never a decode failure that costs the user the whole screen.
    private static func optional<T: Decodable>(
        _ container: KeyedDecodingContainer<Keys>, _ key: Keys, _ type: T.Type
    ) -> T? {
        (try? container.decodeIfPresent(type, forKey: key)) ?? nil
    }
}

/// `GET /api/v1/tasks` — the same envelope as `TaskListResponse`, carrying `TaskRecord`s.
struct TaskBoard: Decodable, Sendable {
    let records: [TaskRecord]
    let activeTaskId: String?

    private enum CodingKeys: String, CodingKey { case tasks, activeTaskId = "active_task_id" }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        records = try container.decode([TaskRecord].self, forKey: .tasks)
        activeTaskId = (try? container.decodeIfPresent(String.self, forKey: .activeTaskId)) ?? nil
    }
}

// MARK: - The plan, as a hierarchy

/// The plan the way the backend has modelled it since migration 0045: ONE ordered `seq` space, in which a
/// step's `parent_seq` names its parent. `plan_task` now accepts `{"description", "substeps": [...]}` and
/// writes each sub-step as its own row, so a real plan arrives as a flat, ordered list that happens to
/// carry exactly one level of nesting.
///
/// Two rules this type exists to keep:
///
/// 1. **A flat plan must look exactly as it did before.** With no `parent_seq` anywhere, every step becomes
///    a node with no children — which is the same list, in the same order, drawn the same way.
/// 2. **Nothing is ever dropped.** A child whose parent isn't in this response, or a grandchild in a plan
///    deeper than the backend's own depth-1 bound, is promoted to the top level rather than vanishing into
///    a parent that isn't being drawn.
struct TaskPlan: Sendable {
    struct Node: Identifiable, Sendable {
        let step: TaskStep
        let children: [TaskStep]
        var id: Int { step.seq }

        var hasChildren: Bool { !children.isEmpty }
        var doneChildren: Int { children.filter(TaskPlan.isSettled).count }
        var failedChildren: Int { children.filter { $0.status == "failed" }.count }
        var runningChild: TaskStep? { children.first { $0.status == "running" } }
        /// The next child that hasn't started — what this group does after the one in hand.
        var nextChild: TaskStep? {
            children.first { ["pending", "waiting", "blocked"].contains($0.status) }
        }
        /// 0…1 from the children's OWN statuses. A parent's progress is its children's progress; when it
        /// has none, it is its own single step, done or not.
        var progress: Double {
            guard hasChildren else { return TaskPlan.isSettled(step) ? 1 : 0 }
            return Double(doneChildren) / Double(children.count)
        }
        /// Everything under this node has settled — the whole group is behind us.
        var isSettled: Bool { TaskPlan.isSettled(step) && children.allSatisfy(TaskPlan.isSettled) }
        var isLive: Bool { step.status == "running" || runningChild != nil }
        var needsAttention: Bool { step.status == "failed" || failedChildren > 0 }
        /// Every real step this node stands for — itself plus its children — for counting.
        var allSteps: [TaskStep] { [step] + children }
    }

    let nodes: [Node]
    /// True when the backend actually sent a hierarchy. Used only to decide whether to SAY anything about
    /// sub-steps; the rendering itself is identical either way.
    let isNested: Bool

    static func isSettled(_ step: TaskStep) -> Bool { step.status == "done" || step.status == "skipped" }

    init(steps: [TaskStep], parents: [Int: Int]) {
        let known = Set(steps.map(\.seq))
        var parentOf: [Int: Int] = [:]
        for step in steps {
            guard let parent = parents[step.seq], parent != step.seq, known.contains(parent) else { continue }
            parentOf[step.seq] = parent
        }
        // Depth-1, like the backend: a step whose parent is itself a child stands on its own rather than
        // being nested into a lane that is never drawn.
        let childSeqs = Set(parentOf.keys)
        for seq in parentOf.filter({ childSeqs.contains($0.value) }).map(\.key) {
            parentOf.removeValue(forKey: seq)
        }

        var childrenBySeq: [Int: [TaskStep]] = [:]
        var top: [TaskStep] = []
        for step in steps {
            if let parent = parentOf[step.seq] {
                childrenBySeq[parent, default: []].append(step)
            } else {
                top.append(step)
            }
        }

        nodes = top.map { step in
            Node(step: step, children: (childrenBySeq[step.seq] ?? []).sorted { $0.seq < $1.seq })
        }
        isNested = !childrenBySeq.isEmpty
    }
}

/// "Is this healthy?", as one word. Every case is reached from a real field — a status the backend set, a
/// timestamp it stamped, an attempt count it incremented, or the watchdog's own published verdict. There is
/// deliberately no case that means "probably fine".
enum TaskHealth: Equatable, Sendable {
    case running          // status=running, heartbeat fresh
    case waitingOnYou     // status=waiting / waiting_for_user
    case retrying         // the step in hand is on attempt ≥ 2
    case stalled          // watchdog says potentially_stuck/orphaned, or the heartbeat has gone stale
    case interrupted      // interrupted_at is set — the process died mid-run, recovery pending
    case paused           // status=paused
    case queued           // status=open — planned, not started
    case blocked          // status=blocked
    case done             // status=done
    case failed           // status=failed
    case closed           // abandoned / cancelled / superseded
    case unknown(String)  // a status this build doesn't model — shown verbatim, never guessed at

    var label: String {
        switch self {
        case .running: "Running"
        case .waitingOnYou: "Waiting on you"
        case .retrying: "Retrying"
        case .stalled: "Stalled"
        case .interrupted: "Interrupted"
        case .paused: "Paused"
        case .queued: "Queued"
        case .blocked: "Blocked"
        case .done: "Done"
        case .failed: "Failed"
        case .closed: "Closed"
        case .unknown(let raw): raw.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    /// Ink for the normal case; a muted tone only where the user is genuinely wanted (item 12).
    var color: Color {
        switch self {
        case .running: Theme.Colors.accent
        case .waitingOnYou, .stalled, .interrupted, .retrying, .blocked: Theme.Colors.warn
        case .failed: Theme.Colors.danger
        case .done: Theme.Colors.secondaryText
        case .paused, .queued, .closed, .unknown: Theme.Colors.idle
        }
    }

    /// Shape carries the meaning too, so the row reads without color perception.
    var symbol: String {
        switch self {
        case .running: "play.circle.fill"
        case .waitingOnYou: "questionmark.circle.fill"
        case .retrying: "arrow.triangle.2.circlepath"
        case .stalled: "exclamationmark.triangle.fill"
        case .interrupted: "bolt.horizontal.circle.fill"
        case .paused: "pause.circle.fill"
        case .queued: "circle.dotted"
        case .blocked: "hand.raised.fill"
        case .done: "checkmark.circle.fill"
        case .failed: "xmark.circle.fill"
        case .closed: "moon.zzz.fill"
        case .unknown: "circle"
        }
    }

    var needsYou: Bool { self == .waitingOnYou }
    /// Running-but-unwell: worth surfacing in the state-of-play strip without pretending it's on the user.
    var needsALook: Bool {
        switch self {
        case .stalled, .interrupted, .failed, .blocked: true
        default: false
        }
    }
    var isLive: Bool {
        switch self {
        case .running, .retrying: true
        default: false
        }
    }
}

/// Everything the pinned header and the list row need, computed once from real state.
struct TaskVitals: Sendable {
    let health: TaskHealth
    /// What the task is doing RIGHT NOW, in one line — the running step, else what is next.
    let live: String
    /// The single supporting fact that explains the health verdict, when there is one.
    let note: String?
    /// 1-based position of the step in hand within the plan.
    let stepIndex: Int?
    let stepCount: Int
    let doneCount: Int
    let failedCount: Int
    let verifiedCount: Int
    /// When the step in hand is a SUB-step, the description of the parent it belongs to, and how far that
    /// group has got. Both come from real `parent_seq` rows; nil for a flat plan.
    let groupTitle: String?
    let groupDone: Int
    let groupCount: Int

    var stepCaption: String? {
        guard stepCount > 0 else { return nil }
        if let stepIndex { return "Step \(stepIndex) of \(stepCount)" }
        return "\(doneCount) of \(stepCount) steps done"
    }

    /// "2 of 4 in this group" — only ever shown when the plan really is nested.
    var groupCaption: String? {
        guard groupCount > 0 else { return nil }
        return "\(groupDone) of \(groupCount) in this group"
    }

    /// Settled work as a fraction of the plan. Zero steps means there is no plan to be a fraction of.
    var progress: Double {
        guard stepCount > 0 else { return 0 }
        return Double(doneCount) / Double(stepCount)
    }

    /// The backend's own watchdog threshold: no heartbeat for 5 minutes is `orphaned` there, so the app
    /// uses the same number rather than inventing a second definition of "stalled".
    static let heartbeatTimeout: TimeInterval = 300

    /// - Parameter watchdog: the newest `task.health_changed` verdict the app has seen for this task
    ///   (`healthy` | `active_tool` | `potentially_stuck` | `orphaned`). The backend's word beats any
    ///   client-side inference.
    static func read(_ task: TaskSummary, facts: TaskFacts,
                     watchdog: String? = nil, now: Date = Date()) -> TaskVitals {
        let steps = task.steps
        // Turn 7: prefer backend-authoritative counts. `_compute_tally` runs the exact same
        // SQL every event carries, so this can never drift from what the store's
        // auto-complete predicate saw. Client fallback matches the tally shape byte-for-byte.
        let done = task.doneSteps.map { $0 + (task.skippedSteps ?? 0) }
                   ?? steps.filter { $0.status == "done" || $0.status == "skipped" }.count
        let failed = steps.filter { $0.status == "failed" }.count
        let verified = task.verifiedSteps ?? steps.filter(\.verified).count

        // A CONTAINER row (a parent step with sub-steps) is never "worked" directly — its children are,
        // and it auto-completes when they settle. So it must not be picked as the running/next step or
        // the header would point "Next: …" and "Step X of N" at a grouping row instead of a real step
        // (the backend leaves the parent 'pending' while its leaves run, one at a time).
        let containerSeqs = Set(facts.parents.values)
        func isContainer(_ s: TaskStep) -> Bool { containerSeqs.contains(s.seq) }
        let running = steps.first { $0.status == "running" && !isContainer($0) }
        let firstFailed = steps.first { $0.status == "failed" && !isContainer($0) }
        let upNext = steps.first {
            !isContainer($0) && ["pending", "waiting", "blocked"].contains($0.status)
        }
        let focus = running ?? firstFailed ?? upNext
        let index = focus.flatMap { step in steps.firstIndex { $0.seq == step.seq }.map { $0 + 1 } }

        let health = health(for: task, facts: facts, watchdog: watchdog, running: running, now: now)

        // The group the step in hand belongs to, when the plan is nested (migration 0045). Read from the
        // real `parent_seq` map — a step with no parent simply has no group.
        var groupTitle: String?
        var groupDone = 0
        var groupCount = 0
        if let focus, let parentSeq = facts.parents[focus.seq],
           let parent = steps.first(where: { $0.seq == parentSeq }) {
            let siblings = steps.filter { facts.parents[$0.seq] == parentSeq }
            groupTitle = parent.description
            groupCount = siblings.count
            groupDone = siblings.filter(TaskPlan.isSettled).count
        }

        return TaskVitals(
            health: health,
            live: liveLine(task: task, health: health, running: running, upNext: upNext),
            note: note(task: task, facts: facts, health: health, running: running,
                       watchdog: watchdog, now: now),
            stepIndex: index,
            stepCount: steps.count,
            doneCount: done,
            failedCount: failed,
            verifiedCount: verified,
            groupTitle: groupTitle,
            groupDone: groupDone,
            groupCount: groupCount
        )
    }

    private static func health(for task: TaskSummary, facts: TaskFacts, watchdog: String?,
                               running: TaskStep?, now: Date) -> TaskHealth {
        switch task.state {
        case .done: return .done
        case .failed: return .failed
        case .abandoned, .cancelled, .superseded: return .closed
        case .waiting, .waitingForUser: return .waitingOnYou
        case .blocked, .blockedByReview: return .blocked   // Turn 8: same warn tier
        // Turn 8: rework-loop visibility. `needsChanges` gets the same visual tier as
        // waiting-on-you — it needs the user's OR the model's attention. Distinct from
        // .blocked (external block) semantically, mapped to the same TaskHealth pill.
        case .needsChanges: return .waitingOnYou
        case .paused: return facts.interruptedAt != nil ? .interrupted : .paused
        case .other: return .unknown(task.status)
        case .open, .running:
            if facts.interruptedAt != nil { return .interrupted }
            if watchdog == "orphaned" || watchdog == "potentially_stuck" { return .stalled }
            if let beat = facts.lastHeartbeat, task.state == .running,
               now.timeIntervalSince(beat) > heartbeatTimeout { return .stalled }
            if (running?.attempts ?? 0) >= 2 { return .retrying }
            return task.state == .running ? .running : .queued
        }
    }

    private static func liveLine(task: TaskSummary, health: TaskHealth,
                                 running: TaskStep?, upNext: TaskStep?) -> String {
        if let running { return running.description }
        switch health {
        case .waitingOnYou: return "Parked on a question for you"
        case .done: return task.result?.taskFirstLine ?? "Finished — see the output"
        case .failed: return task.steps.compactMap(\.lastError).last?.taskFirstLine ?? "Stopped after a failure"
        case .closed: return "No longer active"
        default: break
        }
        if let upNext { return "Next: \(upNext.description)" }
        if let result = task.result?.taskFirstLine { return result }
        return task.steps.isEmpty ? "No steps planned yet" : "Nothing in flight"
    }

    private static func note(task: TaskSummary, facts: TaskFacts, health: TaskHealth,
                             running: TaskStep?, watchdog: String?, now: Date) -> String? {
        if let interrupted = facts.interruptedAt {
            var text = "Interrupted \(taskAge(now.timeIntervalSince(interrupted))) ago"
            if facts.retryCount > 0 {
                text += " · \(facts.retryCount) of \(facts.maxRetries) recoveries used"
            }
            return text
        }
        if health == .stalled {
            if let beat = facts.lastHeartbeat {
                return "No heartbeat for \(taskAge(now.timeIntervalSince(beat)))"
            }
            return watchdog == "potentially_stuck"
                ? "Watchdog: no meaningful progress recently"
                : "Watchdog: no heartbeat recorded"
        }
        if health == .retrying, let running {
            return "Attempt \(running.attempts) of this step"
        }
        if facts.retryCount > 0 {
            return "Resumed \(facts.retryCount)× after interruption (max \(facts.maxRetries))"
        }
        if health.isLive, let beat = facts.lastHeartbeat {
            return "Heartbeat \(taskAge(now.timeIntervalSince(beat))) ago"
        }
        if let updated = task.updatedAt { return "Updated \(updated.saliRelative)" }
        return nil
    }
}

/// Compact age ("40s", "12m", "3h", "2d"). A duration, not a date — `saliRelative` answers "when", this
/// answers "for how long", which is the question a heartbeat asks.
func taskAge(_ interval: TimeInterval) -> String {
    let seconds = max(0, Int(interval))
    if seconds < 60 { return "\(seconds)s" }
    if seconds < 3600 { return "\(seconds / 60)m" }
    if seconds < 86_400 { return "\(seconds / 3600)h" }
    return "\(seconds / 86_400)d"
}

extension String {
    /// The first non-empty line, trimmed — for putting a multi-line result into a single-line slot.
    var taskFirstLine: String? {
        let line = split(separator: "\n").first { !$0.trimmingCharacters(in: .whitespaces).isEmpty }
        guard let line else { return nil }
        return String(line).trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

// MARK: - Shared task chrome

/// The plan at a glance: one segment per step, in order. Progress is carried by FILL — settled work is ink
/// at half strength, the step in hand is full ink and breathes, what's left is a hairline track, and a
/// failure is the one thing that takes a tone.
///
/// A long plan is bucketed rather than truncated: with more steps than segments, each segment covers
/// `ceil(count / segments)` consecutive steps and takes the most urgent state in its bucket
/// (failed → running → done → pending), so the rail never claims a step is finished when it isn't.
struct TaskStepRail: View {
    let steps: [TaskStep]
    var height: CGFloat = Theme.Spacing.xs
    var segments: Int = 24

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var breathing = false

    private enum Segment { case done, running, failed, pending }

    var body: some View {
        let buckets = self.buckets
        let isLive = buckets.contains(.running)
        return HStack(spacing: Theme.Spacing.xs) {
            ForEach(Array(buckets.enumerated()), id: \.offset) { _, segment in
                Capsule(style: .continuous)
                    .fill(fill(segment))
                    .opacity(segment == .running && breathing ? 0.45 : 1)
                    .frame(maxWidth: .infinity)
            }
        }
        .frame(height: height)
        // A step landing is the one moment this view has to SHOW something: the fill has to travel, not
        // appear. Gated through `honoring`, so the new state still lands instantly under Reduce Motion.
        .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.gentle), value: buckets)
        // The ambient loop is bound to the live state, not to appearance: a task that starts running while
        // its row is already on screen must start breathing, and one that finishes must stop.
        .onAppear { syncBreathing(isLive) }
        .onChange(of: isLive) { _, live in syncBreathing(live) }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Plan progress")
        .accessibilityValue(accessibilityValue)
    }

    /// The full ambient pattern from `Theme.Motion`: under Reduce Motion the driving state is never set at
    /// all, so the rail rests at full strength rather than freezing mid-fade.
    private func syncBreathing(_ isLive: Bool) {
        guard !reduceMotion else { return }
        if isLive {
            guard !breathing else { return }
            withAnimation(Theme.Motion.breathing) { breathing = true }
        } else if breathing {
            withAnimation(Theme.Motion.quick) { breathing = false }
        }
    }

    private var buckets: [Segment] {
        guard !steps.isEmpty else { return [] }
        let size = max(1, Int((Double(steps.count) / Double(segments)).rounded(.up)))
        return stride(from: 0, to: steps.count, by: size).map { start in
            let slice = steps[start..<min(start + size, steps.count)]
            if slice.contains(where: { $0.status == "failed" }) { return .failed }
            if slice.contains(where: { $0.status == "running" }) { return .running }
            if slice.allSatisfy({ $0.status == "done" || $0.status == "skipped" }) { return .done }
            return .pending
        }
    }

    private func fill(_ segment: Segment) -> Color {
        switch segment {
        case .done: Theme.Colors.accent.opacity(0.55)
        case .running: Theme.Colors.accent
        case .failed: Theme.Colors.danger
        case .pending: Theme.Colors.separator
        }
    }

    private var accessibilityValue: String {
        let done = steps.filter { $0.status == "done" || $0.status == "skipped" }.count
        let failed = steps.filter { $0.status == "failed" }.count
        var text = "\(done) of \(steps.count) steps done"
        if failed > 0 { text += ", \(failed) failed" }
        return text
    }
}

/// The "this is happening now" mark. A single ambient beat from `Theme.Motion`, and nothing at all when the
/// user has asked for stillness — the driving state is never set, so it rests filled rather than mid-fade.
struct TaskLiveDot: View {
    /// The dot is used at two scales — a row's live mark, and a sub-step's marker inside a group.
    var size: CGFloat = Theme.Spacing.s

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var dimmed = false

    var body: some View {
        Circle()
            .fill(Theme.Colors.accent)
            .frame(width: size, height: size)
            .opacity(dimmed ? 0.3 : 1)
            .onAppear {
                guard !reduceMotion, !dimmed else { return }
                withAnimation(Theme.Motion.pulse) { dimmed = true }
            }
            .accessibilityHidden(true)
    }
}

/// The ambient halo behind the step in hand — a single slow swell on `Motion.beat`, drawn as a ring around
/// whatever it wraps. This is the difference between "the screen re-rendered" and "something is happening
/// right now". It is reserved for the step actually in flight and used nowhere else, so what is alive on
/// screen is always the work itself; it breathes on the shared beat, in phase with every other ambient
/// loop in the app.
struct TaskLivePulse<Content: View>: View {
    var radius: CGFloat = Theme.Radius.m
    @ViewBuilder var content: Content

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var swelling = false

    var body: some View {
        content
            .overlay {
                // A moving decoration is not merely frozen for Reduce Motion — it is not in the tree.
                if !reduceMotion {
                    RoundedRectangle(cornerRadius: radius, style: .continuous)
                        .strokeBorder(Theme.Colors.accent.opacity(swelling ? 0.32 : 0.08),
                                      lineWidth: Theme.Stroke.emphasis)
                        .allowsHitTesting(false)
                }
            }
            .onAppear {
                guard !reduceMotion, !swelling else { return }
                withAnimation(Theme.Motion.breathing) { swelling = true }
            }
    }
}

/// The one "nothing here" line inside a section. A bare `Text` row reads as content the user is supposed
/// to act on; this reads as the section saying it has nothing — a quiet statement, and, where there is one,
/// the reason. Used everywhere in this feature so an empty section is never just a sentence floating in a
/// row of its own.
struct TaskQuietNote: View {
    let title: String
    let detail: String?
    let icon: String?

    init(_ title: String, detail: String? = nil, icon: String? = nil) {
        self.title = title
        self.detail = detail
        self.icon = icon
    }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
            if let icon {
                Image(systemName: icon)
                    .font(Theme.Typography.caption)
                    .foregroundStyle(Theme.Colors.tertiaryText)
                    .accessibilityHidden(true)
            }
            VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                Text(title)
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .fixedSize(horizontal: false, vertical: true)
                if let detail {
                    Text(detail)
                        .font(Theme.Typography.metadata)
                        .foregroundStyle(Theme.Colors.tertiaryText)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, Theme.Spacing.xs)
        .accessibilityElement(children: .combine)
    }
}

/// Per-task "last looked at", so the detail screen can answer "what changed while you were away".
/// Local-only and deliberately tiny: a timestamp per task id, nothing about the task itself.
enum TaskSeenStore {
    private static let prefix = "sali.tasks.lastSeen."

    static func lastSeen(_ taskId: String) -> Date? {
        let stamp = UserDefaults.standard.double(forKey: prefix + taskId)
        return stamp > 0 ? Date(timeIntervalSince1970: stamp) : nil
    }

    static func markSeen(_ taskId: String, at date: Date = Date()) {
        UserDefaults.standard.set(date.timeIntervalSince1970, forKey: prefix + taskId)
    }
}

/// Task-scoped event handling shared by the list (health verdicts) and the detail screen (the pulse lane
/// and the away digest).
enum TaskFeed {
    /// Events a person would recognise as activity ON this task. Streaming deltas and the agent's own
    /// "thinking" frames are a debug log, not a pulse, so they never appear.
    static func isPulseWorthy(_ event: SaliEvent) -> Bool {
        if unclassified.contains(event.rawType) { return true }
        switch event.type {
        case .messageDelta, .messageStarted, .messageCompleted, .toolProgress: return false
        default: return !event.type.isBackgroundNoise
        }
    }

    /// Real task events the shared `EventFamily` doesn't classify, because they are written straight to the
    /// event table rather than through `EventPublisher` (`recovery.py::mark_task_interrupted`,
    /// `ledger.py`'s supersede path). Without this they decode to `.other(.unknown)` — i.e. background
    /// noise — and `task.interrupted`, the one event that says "the process died under this task", would
    /// never reach a screen. `task.step_advanced` is deliberately NOT here: `task.progress` is emitted
    /// alongside it and already says the same thing.
    private static let unclassified: Set<String> = ["task.interrupted", "task.decision_superseded"]

    /// The pulse lane's line for an event. Falls through to the shared human summary for everything the
    /// app already knows how to say; only the unclassified events above are phrased here, from what their
    /// emit site actually does — never from their raw name.
    static func summary(for event: SaliEvent) -> String {
        switch event.rawType {
        case "task.interrupted":
            let reason = event.text("reason")?.replacingOccurrences(of: "_", with: " ")
            return reason.map { "Interrupted mid-run · \($0)" } ?? "Interrupted mid-run"
        case "task.decision_superseded":
            return "An earlier decision was replaced"

        // The three highest-frequency frames on a working task, phrased from the payload their emit sites
        // (tasks/store.py `_record_progress` and `advance_step`) actually write. The shared summary maps
        // all three to `.taskProgress`, whose line is "Step: in progress" — so a step LANDING used to read
        // as "in progress", which is the opposite of what happened.
        case "task.progress":
            if let tool = event.text("tool_name") { return "Ran \(tool)" }
            switch event.text("progress_type") {
            case "tool_success": return "A tool finished"
            case "step_advance":
                if let seq = event.int("step_seq") { return "Moved on from step \(seq)" }
                return "Moved on to the next step"
            case "checkpoint": return "Saved a checkpoint mid-step"
            case "compaction": return "Tidied its working memory to keep going"
            case .some(let other) where !other.isEmpty:
                return other.replacingOccurrences(of: "_", with: " ").capitalized
            default: return "Made progress"
            }
        case "task.step.started":
            // Backend: the driver picked this step (pending→running) — the "ongoing step" signal.
            return event.int("step").map { "Started step \($0)" } ?? "Started a step"
        case "task.step.completed":
            let verified = event.bool("verified") == true
            guard let seq = event.int("seq") else { return verified ? "A step finished · verified"
                                                                    : "A step finished" }
            return verified ? "Step \(seq) done · verified" : "Step \(seq) done"
        case "task.step.failed":
            let headline = event.int("seq").map { "Step \($0) failed" } ?? "A step failed"
            if let error = event.text("error") { return "\(headline) · \(error)" }
            return headline
        case "task.finished":
            if let status = event.text("status") { return "Task finished · \(status)" }
            return "The task finished"

        default:
            return event.humanSummary
        }
    }

    /// The durable seed and the live stream, merged on the durable log's monotonic `seq`, newest first —
    /// the same identity rule the Activity feed uses.
    static func merge(seed: [SaliEvent], live: [SaliEvent], taskId: String) -> [SaliEvent] {
        var merged: [String: SaliEvent] = [:]
        for event in seed + live where event.taskId == taskId && isPulseWorthy(event) {
            merged[event.sequence.map { "seq:\($0)" } ?? "id:\(event.id)"] = event
        }
        return merged.values.sorted { left, right in
            if let a = left.sequence, let b = right.sequence, a != b { return a > b }
            let ta = left.timestamp ?? .distantPast
            let tb = right.timestamp ?? .distantPast
            if ta != tb { return ta > tb }
            return left.id > right.id
        }
    }

    /// The newest `task.health_changed` verdict per task, from the live ring. The watchdog publishes
    /// `new_health` on transition only, so this is the backend's own last word about each task.
    static func watchdogVerdicts(_ events: [SaliEvent]) -> [String: String] {
        var verdicts: [String: String] = [:]
        for event in events where event.rawType == "task.health_changed" {
            if let id = event.taskId, let value = event.text("new_health") { verdicts[id] = value }
        }
        return verdicts
    }
}

// MARK: - View model

@MainActor
final class TasksViewModel: ObservableObject {
    enum TaskFilter: String, CaseIterable, Identifiable, Hashable {
        case active = "Active"
        case all = "All"
        case history = "History"
        var id: String { rawValue }
    }

    @Published var boardState: Loadable<TaskBoard> = .idle
    @Published var filter: TaskFilter = .active

    @Published private(set) var historyState: Loadable<TaskHistory> = .idle

    @Published private(set) var revokedTasks: [RevokedTask] = []
    @Published private(set) var revokedLoadFailed = false

    @Published var revivingTaskId: String?
    @Published var reviveError: String?
    @Published var deletingTaskId: String?
    @Published var deleteError: String?

    /// Finished work, ready to render: grouped by the day it finished, plus what the decode had to skip.
    struct TaskHistory: Sendable {
        let days: [TaskHistoryDay]
        let count: Int
        /// Rows the archive returned in a shape this build couldn't read at all. Zero, normally — and said
        /// out loud when it isn't, because a silently shorter list is a lie.
        let skipped: Int
        var isEmpty: Bool { days.isEmpty }
    }

    // MARK: Event-driven refresh, coalesced
    //
    // `task.progress` is emitted on EVERY tool call, so a working task can produce a frame a second. One
    // refresh per frame turned this screen into a request storm (three endpoints × every frame). These
    // three fields collapse a burst into a single refresh, and a burst that arrives DURING a refresh into
    // exactly one more. It is still event-driven — nothing here runs unless Sali did something (§40).

    private var refreshTask: Task<Void, Never>?
    private var refreshPending = false
    private var refreshFull = false
    /// How long a burst is allowed to settle before it costs a request.
    private static let settle: Duration = .milliseconds(250)

    /// Every `task.*` frame, plus the tombstone that moves a task out of the list. Matched on the RAW name
    /// rather than on `EventType`, because the task lifecycle the backend really emits is wider than the
    /// handful of cases the shared enum models — `task.interrupted`, `task.health_changed`,
    /// `task.phase_started`, `task.artifact.created` and friends all land in `.other`, and keying on the
    /// enum silently ignored them.
    static func isTaskEvent(_ event: SaliEvent) -> Bool {
        event.rawType.hasPrefix("task.") || event.type == .intentRevoked
    }

    /// Frames that change what `GET /intent/revoked` or `GET /tasks/history` would answer — i.e. the ones
    /// that MOVE a row between sections. Everything else only changes a task's own state, and re-reading
    /// the board alone is both enough and much cheaper.
    private static let structuralEvents: Set<String> = [
        "task.finished", "task.created", "task.activated", "task.cancelled",
        "task.superseded", "task.decision_superseded", "intent.revoked",
    ]

    func handle(event: SaliEvent, api: APIClient) {
        guard Self.isTaskEvent(event) else { return }
        scheduleRefresh(api: api, full: Self.structuralEvents.contains(event.rawType))
    }

    /// The Turn-5 catch-up hook. AppState's `replayCaughtUpTick` fires from
    /// WebSocketClient.didFinishReplay after a reconnect; this ensures the list re-reads
    /// history + revoked too (a task might have moved between sections while we were offline).
    /// Routes through the same 250ms coalescer as event-driven refreshes, so multi-batch
    /// replays collapse into one reload.
    func onReplayCaughtUp(api: APIClient) {
        scheduleRefresh(api: api, full: true)
    }

    private func scheduleRefresh(api: APIClient, full: Bool) {
        refreshFull = refreshFull || full
        refreshPending = true
        guard refreshTask == nil else { return }
        refreshTask = Task { [weak self] in
            defer { self?.refreshTask = nil }
            while let self, self.refreshPending {
                try? await Task.sleep(for: Self.settle)
                self.refreshPending = false
                let full = self.refreshFull
                self.refreshFull = false
                if full { await self.loadAll(api: api) } else { await self.loadTasks(api: api) }
            }
        }
    }

    func loadAll(api: APIClient) async {
        async let tasksLoad: () = loadTasks(api: api)
        async let revokedLoad: () = loadRevoked(api: api)
        async let historyLoad: () = loadHistory(api: api)
        _ = await (tasksLoad, revokedLoad, historyLoad)
    }

    func loadTasks(api: APIClient) async {
        if case .loaded = boardState {} else { boardState = .loading }
        do {
            let response: TaskBoard = try await api.get("tasks")
            boardState = .loaded(response)
        } catch {
            if boardState.value == nil {
                boardState = .failed((error as? APIError)?.errorDescription ?? "Couldn't load tasks.")
            }
        }
    }

    func loadRevoked(api: APIClient) async {
        do {
            let response: RevokedTasksResponse = try await api.get("intent/revoked")
            revokedTasks = response.revoked
            revokedLoadFailed = false
        } catch {
            // Secondary, informational section — a failure here never blocks active task management.
            revokedLoadFailed = true
        }
    }

    /// Completed work, newest first, grouped by the day it finished. The endpoint answers a bare JSON
    /// array, so the decoded type IS the array — there is no envelope to unwrap — and it is decoded row by
    /// row (`TaskHistoryFeed`) so that no single malformed row can empty the screen.
    func loadHistory(api: APIClient) async {
        if case .loaded = historyState {} else { historyState = .loading }
        do {
            let feed: TaskHistoryFeed = try await api.get("tasks/history", query: ["limit": "50"])
            historyState = .loaded(TaskHistory(days: TaskHistoryDay.group(feed.entries),
                                               count: feed.entries.count,
                                               skipped: feed.skipped))
        } catch {
            // Keep the last-known history on screen through a transient failure, exactly like `loadTasks`.
            if historyState.value == nil {
                historyState = .failed((error as? APIError)?.errorDescription ?? "Couldn't load history.")
            }
        }
    }

    func revive(taskId: String, api: APIClient) async {
        revivingTaskId = taskId
        defer { revivingTaskId = nil }
        do {
            try await api.postVoid("tasks/\(taskId)/revive")
            await loadAll(api: api)
        } catch {
            reviveError = (error as? APIError)?.errorDescription ?? "Couldn't revive that task."
        }
    }

    /// Permanently remove a FINISHED task. Distinct from cancel/abandon (which tombstone a live intent):
    /// this is the tidy-up for work that is already over and just clutters the screen.
    func deleteTask(taskId: String, api: APIClient) async {
        deletingTaskId = taskId
        defer { deletingTaskId = nil }
        do {
            try await api.deleteVoid("tasks/\(taskId)")
            await loadAll(api: api)
        } catch {
            // The server refuses to delete work that is still live and says why — surface its reason
            // verbatim so "cancel it first" actually reaches the person instead of a generic failure.
            deleteError = (error as? APIError)?.errorDescription ?? "Couldn't delete that task."
        }
    }
}

// MARK: - Screen

struct TasksView: View {
    @EnvironmentObject private var appState: AppState
    @StateObject private var viewModel = TasksViewModel()
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var records: [TaskRecord] { viewModel.boardState.value?.records ?? [] }
    private var activeTaskId: String? { viewModel.boardState.value?.activeTaskId }

    /// One vitals read per task per render, so the groups, the strip and the rows can never disagree.
    private var vitals: [String: TaskVitals] {
        let verdicts = TaskFeed.watchdogVerdicts(appState.liveEvents)
        return records.reduce(into: [:]) { result, record in
            result[record.id] = TaskVitals.read(record.task, facts: record.facts,
                                                watchdog: verdicts[record.id])
        }
    }

    private func sorted(_ items: [TaskRecord]) -> [TaskRecord] {
        items.sorted { ($0.task.updatedAt ?? .distantPast) > ($1.task.updatedAt ?? .distantPast) }
    }

    var body: some View {
        NavigationStack {
            content
                // Titled from the tab itself, so the screen and its tab item can never drift apart —
                // this view IS the Work tab's root (App/RootView).
                .navigationTitle(AppTab.work.title)
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    // Presence, not the raw socket. This slot used to hold a ConnectionBadge reading
                    // "Live" — a second vocabulary for the same corner of the same app (the Inbox
                    // shows "IDLE" here), and a claim about the transport rather than about Sali:
                    // "Live" sat next to "0 Running", which is exactly backwards. Presence already
                    // collapses to `.offline` when the socket drops, so nothing is lost. It also went in
                    // as a bare chip, which is what got it wrapped in the toolbar's own glass — the
                    // shared toolbar item is the only thing that suppresses that.
                    PresenceToolbarItem()
                }
                .safeAreaInset(edge: .top, spacing: 0) { controlBar }
        }
        .task { await viewModel.loadAll(api: appState.api) }
        .onChange(of: appState.latestEvent?.id) { _, _ in
            // Event-driven, not time-driven (§40): a row's state/progress/updated-at goes stale the moment
            // Sali advances a task, so any task lifecycle frame re-pulls the list — never a polling timer.
            // The view model decides what that costs (a burst of frames is one refresh) and how wide it
            // goes (only the frames that move a row between sections re-read history + revoked).
            guard let event = appState.latestEvent else { return }
            viewModel.handle(event: event, api: appState.api)
        }
        .onChange(of: appState.replayCaughtUpTick) { _, _ in
            // Turn 5: after a WebSocket reconnect + replay, latestEvent stays untouched (replayed
            // events are gated out) — so this screen was stale until pull-to-refresh. Widen the
            // reload to `full: true` so history + revoked list are also caught up. Multi-batch
            // gaps fire this multiple times; the 250ms scheduleRefresh settle collapses them.
            viewModel.onReplayCaughtUp(api: appState.api)
        }
        .onChange(of: viewModel.filter) { _, filter in
            // Switching TO history is a request to see the archive as it is now — the one refresh the user
            // actually asked for. Everything else on this screen is driven by events.
            guard filter == .history else { return }
            Task { await viewModel.loadHistory(api: appState.api) }
        }
        .alert("Couldn't delete that task", isPresented: deleteErrorBinding) {
            Button("OK", role: .cancel) { viewModel.deleteError = nil }
        } message: {
            Text(viewModel.deleteError ?? "")
        }
        .alert("Couldn't revive that task", isPresented: reviveErrorBinding) {
            Button("OK", role: .cancel) { viewModel.reviveError = nil }
        } message: {
            Text(viewModel.reviveError ?? "")
        }
    }

    private var deleteErrorBinding: Binding<Bool> {
        Binding(get: { viewModel.deleteError != nil }, set: { if !$0 { viewModel.deleteError = nil } })
    }

    private var reviveErrorBinding: Binding<Bool> {
        Binding(get: { viewModel.reviveError != nil }, set: { if !$0 { viewModel.reviveError = nil } })
    }

    @ViewBuilder private var content: some View {
        // History comes from the sali-works ARCHIVE, not from `GET /tasks` — so it has to render even
        // while the board is loading, and especially when the board has failed. Gating the whole screen on
        // the board meant a tasks outage also blanked the one tab that exists to outlive a task.
        if viewModel.filter == .history {
            taskList
        } else if viewModel.boardState.value == nil {
            switch viewModel.boardState {
            case .failed(let message):
                ErrorStateView(message) { Task { await viewModel.loadAll(api: appState.api) } }
            default:
                SaliSkeletonList(rows: 5, showsBar: true, lines: 1)
            }
        } else {
            taskList
        }
    }

    // MARK: Fixed control bar (never scrolls away)

    /// Filter + state of play, pinned. The filter used to be the first row of the list, which meant the one
    /// control that changes what the screen is showing scrolled off the top of it.
    private var controlBar: some View {
        VStack(spacing: Theme.Spacing.m) {
            Picker("Filter", selection: $viewModel.filter) {
                ForEach(TasksViewModel.TaskFilter.allCases) { filter in
                    Text(filter.rawValue).tag(filter)
                }
            }
            .pickerStyle(.segmented)
            .accessibilityLabel("Task filter")

            if viewModel.filter != .history, viewModel.boardState.value != nil {
                stateOfPlay
            }
        }
        .padding(.horizontal, Theme.Spacing.listMargin)
        .padding(.top, Theme.Spacing.s)
        .padding(.bottom, Theme.Spacing.m)
        .background(Theme.Colors.background)
        .overlay(alignment: .bottom) {
            Rectangle()
                .fill(Theme.Colors.separator)
                .frame(height: Theme.Stroke.hairline)
        }
    }

    /// Counts, not adjectives. Every number is a count of tasks in a state the backend actually reported.
    private var stateOfPlay: some View {
        let reads = vitals
        let active = records.filter { $0.task.state.isActive }
        let live = active.filter { reads[$0.id]?.health.isLive == true }.count
        let waiting = active.filter { reads[$0.id]?.health.needsYou == true }.count
        let look = active.filter { reads[$0.id]?.health.needsALook == true }.count

        return HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.xl) {
            statCell(live, "Running")
            statCell(waiting, "Waiting on you", tint: waiting > 0 ? Theme.Colors.warn : nil)
            if look > 0 { statCell(look, "Needs a look", tint: Theme.Colors.warn) }
            Spacer(minLength: 0)
            if let activeTaskId, records.contains(where: { $0.id == activeTaskId }) {
                Label("Primary set", systemImage: "star.fill")
                    .font(Theme.Typography.metadata)
                    .foregroundStyle(Theme.Colors.tertiaryText)
            }
        }
        .accessibilityElement(children: .combine)
    }

    private func statCell(_ value: Int, _ label: String, tint: Color? = nil) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.xs) {
            Text("\(value)")
                .font(Theme.Typography.numeralSmall)
                .foregroundStyle(tint ?? Theme.Colors.primaryText)
            Text(label)
                .font(Theme.Typography.metadata)
                .foregroundStyle(tint ?? Theme.Colors.tertiaryText)
        }
    }

    // MARK: Loaded content

    private var taskList: some View {
        // One read per task, computed once and handed to both groups — the strip, the "Needs you" group and
        // the rows must never disagree about a task's health.
        let reads = vitals
        return List {
            if viewModel.filter == .history {
                historySection
            } else {
                needsYouSection(reads)
                workingSection(reads)
                revokedSection
            }
        }
        .listStyle(.insetGrouped)
        .saliList()
        .refreshable { await viewModel.loadAll(api: appState.api) }
        // A task finishing MOVES its row out of the list, and a step landing changes what a row says.
        // Both arrive as a silent re-render otherwise; the fingerprint is real state, so a refresh that
        // changed nothing animates nothing.
        .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.standard), value: boardFingerprint)
    }

    /// Every task's identity and settled-step count, in order.
    private var boardFingerprint: String {
        records.map { record in
            let done = record.task.steps.filter { TaskPlan.isSettled($0) }.count
            return "\(record.id):\(record.task.status):\(done)/\(record.task.steps.count)"
        }
        .joined(separator: "|")
    }

    /// Triage, pinned at the top: tasks the backend has parked ON THE USER. Nothing else earns this spot —
    /// a stalled task is Sali's problem to report, a waiting one is yours to answer.
    @ViewBuilder private func needsYouSection(_ reads: [String: TaskVitals]) -> some View {
        let waiting = sorted(records.filter { reads[$0.id]?.health.needsYou == true })
        if !waiting.isEmpty {
            Section {
                ForEach(waiting) { record in
                    row(for: record, vitals: reads[record.id])
                }
            } header: {
                SectionHeader("Needs you",
                              subtitle: "Sali stopped here and is waiting for your answer")
            }
        }
    }

    @ViewBuilder private func workingSection(_ reads: [String: TaskVitals]) -> some View {
        let pool = viewModel.filter == .all ? records : records.filter { $0.task.state.isActive }
        let rest = sorted(pool.filter { reads[$0.id]?.health.needsYou != true })

        Section {
            if rest.isEmpty {
                TaskQuietNote(emptyMessage, detail: emptyDetail, icon: "checkmark.circle")
            } else {
                ForEach(rest) { record in
                    row(for: record, vitals: reads[record.id])
                }
            }
        } header: {
            SectionHeader(viewModel.filter == .active ? "Working" : "All tasks",
                          subtitle: rest.isEmpty ? nil : "Newest activity first")
        }
    }

    private var emptyMessage: String {
        viewModel.filter == .active ? "Nothing running right now." : "No tasks recorded yet."
    }

    private var emptyDetail: String? {
        viewModel.filter == .active
            ? "Finished work moves to History; anything abandoned stays below."
            : "Ask Sali for something in Chat and the task it plans appears here."
    }

    private func row(for record: TaskRecord, vitals: TaskVitals?) -> some View {
        NavigationLink {
            TaskDetailView(taskId: record.id)
        } label: {
            TaskRow(record: record,
                    vitals: vitals ?? TaskVitals.read(record.task, facts: record.facts),
                    isPrimary: record.id == activeTaskId)
        }
    }

    @ViewBuilder private var revokedSection: some View {
        if !viewModel.revokedTasks.isEmpty || viewModel.revokedLoadFailed {
            Section {
                if viewModel.revokedLoadFailed && viewModel.revokedTasks.isEmpty {
                    HStack {
                        Text("Couldn't load historical tasks.")
                            .font(Theme.Typography.footnote)
                            .foregroundStyle(Theme.Colors.secondaryText)
                        Spacer()
                        Button("Try again") { Task { await viewModel.loadRevoked(api: appState.api) } }
                            .font(Theme.Typography.footnote)
                            .frame(minWidth: 44, minHeight: 44)
                            .contentShape(Rectangle())
                    }
                } else {
                    ForEach(viewModel.revokedTasks) { item in
                        RevokedTaskRow(
                            item: item,
                            canControl: appState.role.canControl,
                            isReviving: viewModel.revivingTaskId == item.taskId
                        ) {
                            Task { await viewModel.revive(taskId: item.taskId, api: appState.api) }
                        }
                    }
                }
            } header: {
                SectionHeader("Historical / abandoned",
                              subtitle: "Not currently active — will not auto-resume")
            }
        }
    }

    /// Finished work from the archive, grouped by the day it finished. Rows are deliberately NOT
    /// `NavigationLink`s: the task record is deleted from the database the moment work completes, so
    /// `GET /tasks/{id}` 404s for every one of these — tapping expands the row in place instead.
    ///
    /// One `Section` PER DAY, so each day gets its own grouped card and its own header rather than a
    /// wall of rows with headings buried inside it.
    @ViewBuilder private var historySection: some View {
        switch viewModel.historyState {
        case .idle, .loading:
            Section {
                ForEach(0..<3, id: \.self) { _ in
                    SaliSkeletonRow(lines: 1, showsBar: false)
                        .padding(.vertical, Theme.Spacing.xs)
                }
            } header: {
                SectionHeader("History", subtitle: "Reading the archive…")
            }

        case .failed(let message):
            Section {
                ErrorStateView(message) { Task { await viewModel.loadHistory(api: appState.api) } }
            } header: {
                SectionHeader("History", subtitle: historySubtitle)
            }

        case .loaded(let history) where history.isEmpty:
            Section {
                TaskQuietNote("No finished work yet.",
                              detail: "When Sali finishes a task, it is archived here — what it set out to "
                                    + "do, what came of it, and where it ran.",
                              icon: "archivebox")
            } header: {
                SectionHeader("History", subtitle: historySubtitle)
            }

        case .loaded(let history):
            ForEach(Array(history.days.enumerated()), id: \.element.id) { index, day in
                Section {
                    ForEach(day.entries) { entry in
                        HistoryTaskRow(entry: entry)
                            // Finished work is the only work that can be deleted, and this whole section
                            // IS finished work — so the gesture is offered here and nowhere else. Full
                            // swipe is off: removing a record permanently should take a deliberate tap.
                            .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                                if let tid = entry.taskId {
                                    Button(role: .destructive) {
                                        Task { await viewModel.deleteTask(taskId: tid, api: appState.api) }
                                    } label: {
                                        Label("Delete", systemImage: "trash")
                                    }
                                }
                            }
                    }
                } header: {
                    if index == 0 {
                        VStack(alignment: .leading, spacing: 0) {
                            SectionHeader("History", subtitle: historySubtitle)
                            SectionHeader(day.title, subtitle: day.detail, emphasis: .secondary)
                        }
                    } else {
                        SectionHeader(day.title, subtitle: day.detail, emphasis: .secondary)
                    }
                }
            }
            SectionFooter(historyFootnote(history),
                          detail: "\(history.count) archived")
        }
    }

    private var historySubtitle: String { "Finished work, newest first — archived, not resumable" }

    /// Says where these rows come from, and — when it happens — that the list is SHORTER than the archive.
    /// A quietly truncated history is the same failure as an empty one, just harder to notice.
    private func historyFootnote(_ history: TasksViewModel.TaskHistory) -> String {
        var text = "Read from the sali-works archive. The task record itself is deleted when the work "
                 + "completes, so finished work can be read here but never reopened."
        if history.skipped > 0 {
            text += " \(history.skipped) row\(history.skipped == 1 ? "" : "s") in the archive couldn't be "
                  + "read by this version of the app and aren't shown."
        }
        return text
    }
}

// MARK: - Rows

/// A task, built around what it is DOING. One rhythm, always in the same order, so a column of them scans:
///
///   objective ─────────────────────── state
///   ● what it is doing right now
///   ▄▄▄▄▄▄▄▄▄▄░░░░░░░░░░░░░░░░░░░  3/12
///   group · workspace ─────── the one supporting fact
///
/// The rail and the count share a line because they are the same fact at two resolutions; putting the count
/// on its own line made a four-line row read as four unrelated things.
private struct TaskRow: View {
    let record: TaskRecord
    let vitals: TaskVitals
    let isPrimary: Bool

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var task: TaskSummary { record.task }

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            HStack(alignment: .top, spacing: Theme.Spacing.s) {
                Text(task.objective)
                    .font(Theme.Typography.body.weight(.medium))
                    .foregroundStyle(Theme.Colors.primaryText)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: Theme.Spacing.s)
                StatusPill(vitals.health.label, color: vitals.health.color)
            }

            liveLine

            if !task.steps.isEmpty {
                HStack(spacing: Theme.Spacing.m) {
                    TaskStepRail(steps: task.steps)
                    Text("\(vitals.doneCount)/\(vitals.stepCount)")
                        .font(Theme.Typography.numeralSmall)
                        .foregroundStyle(Theme.Colors.tertiaryText)
                        .contentTransition(.numericText())
                        .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.standard),
                                   value: vitals.doneCount)
                        .accessibilityHidden(true)
                }
            }

            metaLine
        }
        .padding(.vertical, Theme.Spacing.xs)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityLabel)
    }

    private var liveLine: some View {
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
                .foregroundStyle(Theme.Colors.secondaryText)
                .lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)
                // The live line is the sentence that changes when Sali moves on. Cross-fade it, so the row
                // shows the change rather than silently containing a different sentence.
                .contentTransition(.opacity)
                .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.gentle), value: vitals.live)
        }
    }

    /// Metadata, in one line and one voice. `stepCaption` lives on the rail now, so this line carries the
    /// things the rail cannot say: which group the work is in (nested plans only), where it is running, and
    /// the single fact that explains the health verdict.
    private var metaLine: some View {
        HStack(spacing: Theme.Spacing.s) {
            if isPrimary {
                Label("Primary", systemImage: "star.fill")
                    .labelStyle(.titleAndIcon)
                    .layoutPriority(1)
            }
            if let group = vitals.groupTitle, let caption = vitals.groupCaption {
                Text("“\(group)” · \(caption)")
                    .lineLimit(1)
                    .truncationMode(.tail)
            }
            if let root = task.workspaceRoot, !root.isEmpty {
                Text(URL(fileURLWithPath: root).lastPathComponent)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            Spacer(minLength: Theme.Spacing.s)
            if let note = vitals.note {
                Text(note)
                    .lineLimit(1)
                    .layoutPriority(1)
            }
        }
        .font(Theme.Typography.metadata)
        .foregroundStyle(Theme.Colors.tertiaryText)
    }

    private var accessibilityLabel: String {
        var parts = [task.objective, vitals.health.label, vitals.live]
        if let caption = vitals.stepCaption { parts.append(caption) }
        if let group = vitals.groupTitle, let caption = vitals.groupCaption {
            parts.append("In \(group), \(caption)")
        }
        if isPrimary { parts.append("Primary task") }
        if let note = vitals.note { parts.append(note) }
        return parts.joined(separator: ". ")
    }
}

private struct RevokedTaskRow: View {
    let item: RevokedTask
    let canControl: Bool
    let isReviving: Bool
    let onRevive: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            Text(item.objective)
                .font(Theme.Typography.body.weight(.medium))
                .lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)

            // The backend stores the reason as a machine token (`user_revoked`), which reached the screen
            // verbatim under the objective. Same treatment the interrupted-run summary already gives it.
            if let reason = Self.reasonText(item.reason) {
                Text(reason)
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.secondaryText)
            }

            HStack(alignment: .center, spacing: Theme.Spacing.s) {
                // NOT "Not currently active — will not auto-resume": that is this section's own subtitle,
                // and repeating it verbatim on every card inside the section says nothing and costs a line.
                // `revoked_at` and `superseded_by` were decoded and never shown, and they are the two facts
                // a tombstone is actually for — when it stopped, and whether something replaced it.
                Label(Self.provenance(item), systemImage: item.supersededBy == nil
                      ? "moon.zzz.fill" : "arrow.triangle.branch")
                    .font(Theme.Typography.metadata)
                    .foregroundStyle(Theme.Colors.tertiaryText)
                    .lineLimit(2)
                Spacer()
                if canControl {
                    if isReviving {
                        ProgressView()
                    } else {
                        Button("Revive", action: onRevive)
                            .buttonStyle(.bordered)
                            // Without this the label renders in the system tint's grey, which in a
                            // monochrome app is indistinguishable from a disabled control — a live action
                            // that looks unavailable.
                            .tint(Theme.Colors.primaryText)
                            .frame(minHeight: 44)
                            .contentShape(Rectangle())
                    }
                }
            }
        }
        .padding(.vertical, Theme.Spacing.xs)
        .accessibilityElement(children: .combine)
    }

    /// The revocation reason as a sentence. `user_revoked` is the common case and deserves to say who did
    /// it; anything else is de-tokenised rather than guessed at.
    static func reasonText(_ raw: String?) -> String? {
        guard let raw, !raw.isEmpty else { return nil }
        switch raw {
        case "user_revoked":       return "You stopped this"
        case "superseded":         return "Replaced by newer work"
        default:
            let spaced = raw.replacingOccurrences(of: "_", with: " ")
            return spaced.prefix(1).uppercased() + spaced.dropFirst()
        }
    }

    /// When it stopped, and whether something took over — the two things the section header cannot say.
    static func provenance(_ item: RevokedTask) -> String {
        if item.supersededBy != nil { return "Superseded by a newer task" }
        guard let at = item.revokedAt, !at.saliIsUnknownTime else { return "Won't auto-resume" }
        return "Stopped \(at.saliRelative)"
    }
}

/// A completed task. Never navigable — the archive row has no live task behind it (§15) — so the tap
/// target expands in place: the full result, the workspace it ran in, when it started and how long it took.
///
/// Every value here is a field the archive really carries. A row whose timestamp this build couldn't read
/// says so, rather than borrowing the day it happens to be filed under.
private struct HistoryTaskRow: View {
    let entry: TaskHistoryEntry
    @State private var isExpanded = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var result: String? {
        guard let result = entry.result, !result.isEmpty else { return nil }
        return result
    }
    private var workspaceRoot: String? {
        guard let root = entry.workspaceRoot, !root.isEmpty else { return nil }
        return root
    }
    /// Nothing hidden means nothing to reveal — don't offer a tap that does nothing.
    private var canExpand: Bool { result != nil || workspaceRoot != nil || entry.createdAt != nil }

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            HStack(alignment: .top, spacing: Theme.Spacing.s) {
                Text(entry.objective)
                    .font(Theme.Typography.body.weight(.medium))
                    .foregroundStyle(Theme.Colors.primaryText)
                    .lineLimit(isExpanded ? nil : 2)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: Theme.Spacing.s)
                StatusPill(entry.outcomeLabel, color: entry.state.color)
            }

            if let result {
                Text(result)
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .lineLimit(isExpanded ? nil : 2)
                    .fixedSize(horizontal: false, vertical: true)
            } else if isExpanded {
                Text("No written result was archived for this task.")
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.tertiaryText)
            }

            factsLine

            if isExpanded {
                VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                    if let root = workspaceRoot {
                        Text(root)
                            .font(Theme.Typography.monoSmall)
                            .foregroundStyle(Theme.Colors.tertiaryText)
                            .lineLimit(3)
                            .truncationMode(.middle)
                    }
                    if let started = entry.createdAt {
                        Text("Started \(started.formatted(date: .abbreviated, time: .shortened))")
                            .font(Theme.Typography.metadata)
                            .foregroundStyle(Theme.Colors.tertiaryText)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .transition(.opacity.combined(with: .move(edge: .top)))
            }
        }
        .padding(.vertical, Theme.Spacing.xs)
        .frame(maxWidth: .infinity, alignment: .leading)
        .contentShape(Rectangle())
        .onTapGesture {
            guard canExpand else { return }
            withAnimation(Theme.Motion.honoring(reduceMotion, Theme.Motion.standard)) { isExpanded.toggle() }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityLabel)
        .accessibilityAddTraits(canExpand ? .isButton : [])
        .accessibilityHint(canExpand ? (isExpanded ? "Shows less" : "Shows the full result") : "")
    }

    /// Where it ran, when it finished, and how long it took — the three questions a history row is for.
    private var factsLine: some View {
        HStack(spacing: Theme.Spacing.s) {
            if let workspaceRoot {
                Label(URL(fileURLWithPath: workspaceRoot).lastPathComponent, systemImage: "folder")
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            Spacer(minLength: Theme.Spacing.s)
            if let duration = entry.duration {
                Text("ran \(taskAge(duration))")
                    .layoutPriority(1)
            }
            Text(finishedText)
                .layoutPriority(2)
        }
        .font(Theme.Typography.metadata)
        .foregroundStyle(Theme.Colors.tertiaryText)
    }

    /// The day is already the section header, so the row only has to say the clock time. A row the archive
    /// couldn't date says exactly that — it is never filed under a time it didn't record.
    private var finishedText: String {
        guard let finishedAt = entry.finishedAt else {
            return entry.hasUnreadableStamp ? "time unreadable" : "time not recorded"
        }
        return finishedAt.formatted(date: .omitted, time: .shortened)
    }

    private var accessibilityLabel: String {
        var parts = [entry.objective, entry.outcomeLabel]
        if let result { parts.append(result) }
        if let workspaceRoot { parts.append("Ran in \(URL(fileURLWithPath: workspaceRoot).lastPathComponent)") }
        if let finishedAt = entry.finishedAt {
            parts.append("Finished \(finishedAt.formatted(date: .abbreviated, time: .shortened))")
        } else {
            parts.append("Finish time not recorded")
        }
        if let duration = entry.duration { parts.append("Ran for \(taskAge(duration))") }
        return parts.joined(separator: ". ")
    }
}

#Preview {
    TasksView().environmentObject(AppState())
}
