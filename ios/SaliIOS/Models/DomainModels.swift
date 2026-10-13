import Foundation

// Domain models — the exact JSON the Sali backend returns (see docs/API_REFERENCE.md). Decoding is lenient
// where the backend omits fields; nothing here invents data. Dates arrive as ISO-8601 strings and are kept
// as `Date?` via a shared decoder (see APIClient).

// MARK: - Auth & devices

public struct IssuedSession: Codable, Sendable, Equatable {
    public let deviceId: String
    public let role: String
    public let accessToken: String
    public let refreshToken: String
    public let accessExpiresAt: Date?
    public let refreshExpiresAt: Date?

    enum CodingKeys: String, CodingKey {
        case deviceId = "device_id", role
        case accessToken = "access_token", refreshToken = "refresh_token"
        case accessExpiresAt = "access_expires_at", refreshExpiresAt = "refresh_expires_at"
    }
}

public struct DeviceInfo: Codable, Identifiable, Sendable {
    public let id: String
    public let name: String
    public let platform: String
    public let model: String?
    public let role: String
    public let status: String            // active | revoked
    public let hasPush: Bool
    public let pushEnvironment: String?
    public let createdAt: Date?
    public let lastSeenAt: Date?
    public let revokedAt: Date?

    enum CodingKeys: String, CodingKey {
        case id, name, platform, model, role, status
        case hasPush = "has_push", pushEnvironment = "push_environment"
        case createdAt = "created_at", lastSeenAt = "last_seen_at", revokedAt = "revoked_at"
    }
    public var isActive: Bool { status == "active" }
}

public struct EnrollCodeResult: Codable, Sendable {
    public let code: String
    public let role: String
    public let expiresAt: Date?
    enum CodingKeys: String, CodingKey { case code, role, expiresAt = "expires_at" }
}

public enum Role: String, Codable, CaseIterable, Sendable {
    case owner, controller, observer
    public var canControl: Bool { self != .observer }
    public var canManageDevices: Bool { self == .owner }
    public var label: String { rawValue.capitalized }
}

// MARK: - Conversation

public struct ConversationMessage: Codable, Identifiable, Sendable, Equatable {
    public let id: String
    public let seq: Int
    public let role: String              // user | assistant | system
    public let content: String
    public let model: String?
    public let createdAt: Date?
    /// A file Sali sent, carried on the history so it renders as a downloadable card and survives a
    /// reload (durable delivery — a live-only task.artifact.created card vanished on refresh).
    public let attachment: MessageAttachmentDTO?
    /// What this reply cost, from `message.token_count` — written on every row since the table
    /// existed and never read back until now. It is what makes the count survive a relaunch: without
    /// it the app can only show a cost for turns it personally watched stream, so every past reply
    /// came back blank. Optional because user rows and older servers may not carry it.
    public let tokenCount: Int?

    enum CodingKeys: String, CodingKey {
        case id, seq, role, content, model, createdAt = "created_at", attachment
        case tokenCount = "token_count"
    }
}

public struct MessageAttachmentDTO: Codable, Sendable, Equatable {
    public let artifactId: String
    public let filename: String
    public let size: Int?
    public let downloadURL: String
    public let kind: String?
    enum CodingKeys: String, CodingKey {
        case artifactId = "artifact_id", filename, size, downloadURL = "download_url", kind
    }
}

public struct ConversationState: Codable, Sendable {
    public let sessionId: String
    public let messages: [ConversationMessage]
    public let summary: String?
    public let lastActive: Date?
    enum CodingKeys: String, CodingKey {
        case sessionId = "session_id", messages, summary, lastActive = "last_active"
    }
}

// MARK: - Tasks

public struct TaskStep: Codable, Identifiable, Sendable {
    public var id: Int { seq }
    public let seq: Int
    public let description: String
    public let status: String
    public let note: String?
    public let attempts: Int
    public let lastError: String?
    public let failureClass: String?
    public let verified: Bool
    // Step-discipline (backend migration 0055): the step as a contract. definitionOfDone is what
    // "done" concretely means (the engine verifies it before accepting the mark); scopeExcludes is
    // what this step must NOT touch. Shown on the task screen so the plan's real shape is visible.
    public let definitionOfDone: String?
    public let scopeExcludes: String?

    enum CodingKeys: String, CodingKey {
        case seq, description, status, note, attempts
        case lastError = "last_error", failureClass = "failure_class", verified
        case definitionOfDone = "definition_of_done", scopeExcludes = "scope_excludes"
    }
}

public struct TaskSummary: Codable, Identifiable, Sendable {
    public let id: String
    public let objective: String
    public let status: String
    public let result: String?
    public let isPrimary: Bool
    public let workspaceRoot: String?
    public let workspaceMode: String
    public let steps: [TaskStep]
    public let createdAt: Date?
    public let updatedAt: Date?
    public let retryCount: Int
    public let maxRetries: Int
    public let supersededBy: String?
    /// Turn 7: backend-authoritative step counts. Populated by the store's `_compute_tally`,
    /// so this is the exact same numerator/denominator the auto-complete predicate uses.
    /// Rendering these verbatim (instead of recomputing client-side) means the iOS progress
    /// bar can never disagree with the backend about whether a task is done. Optional for
    /// API-compat during the rollout window — the fallback matches the new backend contract
    /// (skipped counts as settled, matching `_compute_tally`'s all-complete predicate).
    public let doneSteps: Int?
    public let totalSteps: Int?
    public let verifiedSteps: Int?
    public let skippedSteps: Int?

    enum CodingKeys: String, CodingKey {
        case id, objective, status, result
        case isPrimary = "is_primary", workspaceRoot = "workspace_root", workspaceMode = "workspace_mode"
        case steps, createdAt = "created_at", updatedAt = "updated_at"
        case retryCount = "retry_count", maxRetries = "max_retries", supersededBy = "superseded_by"
        case doneSteps = "done_steps", totalSteps = "total_steps"
        case verifiedSteps = "verified_steps", skippedSteps = "skipped_steps"
    }

    public var state: TaskState { TaskState(rawValue: status) ?? .other }
    public var progress: Double {
        // Prefer backend counts (Turn 7 — one vocabulary). Both `done` and `skipped` count
        // as settled, matching the store's `_compute_tally` all-complete predicate exactly.
        if let total = totalSteps, total > 0 {
            let done = doneSteps ?? 0
            let skipped = skippedSteps ?? 0
            return Double(done + skipped) / Double(total)
        }
        // Legacy fallback for pre-Turn-7 backend responses. Same predicate — never drift.
        guard !steps.isEmpty else { return status == "done" ? 1 : 0 }
        let settled = steps.filter { $0.status == "done" || $0.status == "skipped" }.count
        return Double(settled) / Double(steps.count)
    }
}

public enum TaskState: String, Sendable {
    // NOTE: the backend's `open_tasks` filter includes raw status `"waiting"` (distinct from
    // `"waiting_for_user"`); without this case such a task would decode to `.other` and be dropped
    // from the Active tab.
    case open, running, paused, waiting, waitingForUser = "waiting_for_user", blocked
    // Turn 8: reviewer verdicts land here so the rework loop is visible in the UI.
    // `needsChanges` = reviewer returned NEEDS_REWORK / FAILED (the model has real work
    // to do). `blockedByReview` = reviewer returned BLOCKED (permission, external
    // dependency) — distinct from generic `.blocked` which is a step-level obstruction.
    case needsChanges = "needs_changes"
    case blockedByReview = "blocked_by_review"
    case done, failed, abandoned, cancelled, superseded, other

    /// "Still on Sali's plate" — the exact set the backend's `open_tasks` returns
    /// (`open, running, waiting, blocked, paused`), plus `waiting_for_user`.
    ///
    /// `.paused` belongs here even though nothing is executing. It was missing, and the hole was
    /// reachable: pausing a task from its detail screen made it vanish — the Tasks list filters its
    /// working section on this, `paused` isn't finished either, so the task showed in neither place and
    /// the only way back was the "All" filter. A state you can enter from the UI must never be a state
    /// the UI cannot show you.
    public var isActive: Bool {
        // Turn 8: a task under rework or review-block is very much still on Sali's plate.
        [.open, .running, .blocked, .waiting, .waitingForUser, .paused,
         .needsChanges, .blockedByReview].contains(self)
    }
    public var label: String {
        switch self {
        case .waiting, .waitingForUser: "Waiting for you"
        case .needsChanges: "Needs changes"
        case .blockedByReview: "Blocked by review"
        default: rawValue.capitalized
        }
    }
}

public struct TaskListResponse: Codable, Sendable {
    public let tasks: [TaskSummary]
    public let activeTaskId: String?
    enum CodingKeys: String, CodingKey { case tasks, activeTaskId = "active_task_id" }
}

// MARK: - Artifacts (file transfer)

public struct ArtifactMeta: Codable, Identifiable, Sendable {
    public let id: String
    public let filename: String
    public let artifactType: String
    public let toolName: String?
    public let size: Int?
    public let available: Bool
    public let contentType: String
    public let downloadURL: String        // relative to base; NEVER a host path (§43)

    enum CodingKeys: String, CodingKey {
        case id, filename, artifactType = "artifact_type", toolName = "tool_name"
        case size, available, contentType = "content_type", downloadURL = "download_url"
    }
}

// MARK: - System health

public struct SystemHealth: Codable, Sendable {
    public let model: String
    public let resources: ResourceSnapshot?
    public let health: SubsystemHealth?
    public let websocketConnections: Int
    enum CodingKeys: String, CodingKey {
        case model, resources, health, websocketConnections = "websocket_connections"
    }
}

public struct SubsystemHealth: Codable, Sendable {
    public let allOk: Bool?
    public let datastore: Bool?
    public let model: Bool?
    public let embedder: Bool?
    public let internet: Bool?
    enum CodingKeys: String, CodingKey { case allOk = "all_ok", datastore, model, embedder, internet }
}

/// A live host-resource reading. Fields are optional because a metric that can't be read degrades to nil —
/// never a fabricated number (golden rule).
/// The live `GET /api/v1/system` response (src/sali/api/routes/api.py `get_system`) nests this as
/// `ResourceMonitor.snapshot()` (src/sali/runtime/resources.py): `{"state","reading":{…fractions…},
/// "preserve"}` — or, if the hardware probe itself raises, the route instead returns
/// `{"available": false, "error": "…"}` with no "state"/"reading" at all. Decoding is forgiving in both
/// directions (mirroring the `Experience` model above): the nested fractions become the flat
/// percentage-based fields below, `preserve` → `preservation`, and a missing "reading" degrades every
/// metric to nil rather than guess. The backend only ever reports fractions, never absolute MB, so
/// `vramUsedMB`/`vramTotalMB` are always nil today.
public struct ResourceSnapshot: Sendable {
    public let state: String?             // safe | elevated | high | critical | emergency
    public let preservation: Bool?
    public let gpuUtilPct: Double?
    public let vramUsedPct: Double?
    public let vramUsedMB: Double?
    public let vramTotalMB: Double?
    public let ramUsedPct: Double?
    public let cpuLoad: Double?           // per-core load average, as a percentage (can exceed 100% under overload)
    public let diskUsedPct: Double?
    public let temperatureC: Double?
    public let available: Bool?

    public init(state: String?, preservation: Bool?, gpuUtilPct: Double?, vramUsedPct: Double?,
                vramUsedMB: Double? = nil, vramTotalMB: Double? = nil, ramUsedPct: Double?,
                cpuLoad: Double?, diskUsedPct: Double?, temperatureC: Double?, available: Bool?) {
        self.state = state
        self.preservation = preservation
        self.gpuUtilPct = gpuUtilPct
        self.vramUsedPct = vramUsedPct
        self.vramUsedMB = vramUsedMB
        self.vramTotalMB = vramTotalMB
        self.ramUsedPct = ramUsedPct
        self.cpuLoad = cpuLoad
        self.diskUsedPct = diskUsedPct
        self.temperatureC = temperatureC
        self.available = available
    }

    public var resourceState: ResourceLevel { ResourceLevel(rawValue: state ?? "safe") ?? .safe }
}

extension ResourceSnapshot: Codable {
    // Flat, snake_case keys — the shape /system and /resources actually send (API-layer `_resource_view`).
    private enum FlatKeys: String, CodingKey {
        case state, preservation, available
        case gpuUtilPct = "gpu_util_pct"
        case vramUsedPct = "vram_used_pct"
        case vramUsedMB = "vram_used_mb"
        case vramTotalMB = "vram_total_mb"
        case ramUsedPct = "ram_used_pct"
        case cpuLoad = "cpu_load"
        case diskUsedPct = "disk_used_pct"
        case temperatureC = "temperature_c"
    }
    // Nested runtime fallback (raw ResourceMonitor snapshot).
    private enum RootKeys: String, CodingKey { case state, preserve, reading, available }
    private enum ReadingKeys: String, CodingKey {
        case vramUsedFrac = "vram_used_frac", gpuUtilFrac = "gpu_util_frac", gpuTempC = "gpu_temp_c"
        case ramUsedFrac = "ram_used_frac", cpuLoadPerCore = "cpu_load_per_core"
        case diskUsedFrac = "disk_used_frac", ok
    }

    public init(from decoder: Decoder) throws {
        // Primary shape (what /api/v1/system and /api/v1/resources actually send, via the API-layer
        // `_resource_view` normalizer): FLAT, percentage-based, snake_case — {state, preservation,
        // gpu_util_pct, vram_used_pct, ram_used_pct, disk_used_pct, cpu_load, temperature_c, available}.
        let flat = try decoder.container(keyedBy: FlatKeys.self)
        state = try flat.decodeIfPresent(String.self, forKey: .state)
        vramUsedMB = try flat.decodeIfPresent(Double.self, forKey: .vramUsedMB)
        vramTotalMB = try flat.decodeIfPresent(Double.self, forKey: .vramTotalMB)

        let hasFlatMetrics = flat.contains(.gpuUtilPct) || flat.contains(.vramUsedPct)
            || flat.contains(.preservation)
        if hasFlatMetrics {
            preservation = try flat.decodeIfPresent(Bool.self, forKey: .preservation)
            gpuUtilPct = try flat.decodeIfPresent(Double.self, forKey: .gpuUtilPct)
            vramUsedPct = try flat.decodeIfPresent(Double.self, forKey: .vramUsedPct)
            ramUsedPct = try flat.decodeIfPresent(Double.self, forKey: .ramUsedPct)
            diskUsedPct = try flat.decodeIfPresent(Double.self, forKey: .diskUsedPct)
            cpuLoad = try flat.decodeIfPresent(Double.self, forKey: .cpuLoad)
            temperatureC = try flat.decodeIfPresent(Double.self, forKey: .temperatureC)
            available = try flat.decodeIfPresent(Bool.self, forKey: .available)
            return
        }

        // Fallback: the raw runtime monitor shape {state, reading:{…fractions…}, preserve} — in case a
        // client is pointed at an un-normalized snapshot (e.g. the `raw` field, or a future direct feed).
        let root = try decoder.container(keyedBy: RootKeys.self)
        preservation = try root.decodeIfPresent(Bool.self, forKey: .preserve)
        if let reading = try? root.nestedContainer(keyedBy: ReadingKeys.self, forKey: .reading) {
            let vramFrac = try reading.decodeIfPresent(Double.self, forKey: .vramUsedFrac)
            let gpuFrac = try reading.decodeIfPresent(Double.self, forKey: .gpuUtilFrac)
            let ramFrac = try reading.decodeIfPresent(Double.self, forKey: .ramUsedFrac)
            let diskFrac = try reading.decodeIfPresent(Double.self, forKey: .diskUsedFrac)
            let loadPerCore = try reading.decodeIfPresent(Double.self, forKey: .cpuLoadPerCore)
            gpuUtilPct = gpuFrac.map { $0 * 100 }
            vramUsedPct = vramFrac.map { $0 * 100 }
            ramUsedPct = ramFrac.map { $0 * 100 }
            diskUsedPct = diskFrac.map { $0 * 100 }
            cpuLoad = loadPerCore.map { $0 * 100 }
            temperatureC = try reading.decodeIfPresent(Double.self, forKey: .gpuTempC)
            available = try reading.decodeIfPresent(Bool.self, forKey: .ok)
        } else {
            // The hardware probe itself failed: {"available": false, "error": "…"}. Every metric degrades
            // to nil — the caller renders "—", never a fabricated reading.
            gpuUtilPct = nil; vramUsedPct = nil; ramUsedPct = nil; diskUsedPct = nil
            temperatureC = nil; cpuLoad = nil
            available = try root.decodeIfPresent(Bool.self, forKey: .available)
        }
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: FlatKeys.self)
        try c.encodeIfPresent(state, forKey: .state)
        try c.encodeIfPresent(preservation, forKey: .preservation)
        try c.encodeIfPresent(gpuUtilPct, forKey: .gpuUtilPct)
        try c.encodeIfPresent(vramUsedPct, forKey: .vramUsedPct)
        try c.encodeIfPresent(vramUsedMB, forKey: .vramUsedMB)
        try c.encodeIfPresent(vramTotalMB, forKey: .vramTotalMB)
        try c.encodeIfPresent(ramUsedPct, forKey: .ramUsedPct)
        try c.encodeIfPresent(cpuLoad, forKey: .cpuLoad)
        try c.encodeIfPresent(diskUsedPct, forKey: .diskUsedPct)
        try c.encodeIfPresent(temperatureC, forKey: .temperatureC)
        try c.encodeIfPresent(available, forKey: .available)
    }
}

public enum ResourceLevel: String, Sendable {
    case safe, elevated, high, critical, emergency
    public var meaning: String {
        switch self {
        case .safe:      "System stable"
        case .elevated:  "Load rising — Sali is watching"
        case .high:      "Pressure high — curiosity work yields first"
        case .critical:  "Preservation mode — heavy work paused"
        case .emergency: "Emergency — only your requests run"
        }
    }
}

public struct ResourceIncident: Codable, Identifiable, Sendable {
    public var id: String { "\(kind)-\(createdAt?.timeIntervalSince1970 ?? 0)" }
    public let kind: String
    public let severity: String
    public let workload: String?
    public let mitigation: String?
    public let resolved: Bool?
    public let createdAt: Date?
    enum CodingKeys: String, CodingKey {
        case kind, severity, workload, mitigation, resolved, createdAt = "created_at"
    }
}

// MARK: - Pending questions & consent (async conversation, natural consent)

public struct PendingQuestion: Codable, Identifiable, Sendable {
    public let id: String
    public let question: String
    public let whyItMatters: String?
    public let dependencyKind: String?
    public let personName: String?
    enum CodingKeys: String, CodingKey {
        case id, question, whyItMatters = "why_it_matters"
        case dependencyKind = "dependency_kind", personName = "person_name"
    }
}

public struct ConsentRequest: Codable, Identifiable, Sendable {
    public let id: String
    // ConsentStore.open() (src/sali/tasks/consent.py, backing GET /api/v1/consent) actually selects
    // `id, task_id, action, scope, consequence, judgment_level, created_at` — there is no "summary" column,
    // and "rationale" exists on the table but isn't selected by that query. `summary`/`rationale` are kept
    // (always nil today) only in case a future response shape adds them; `displayText` below is what the
    // UI should actually show.
    public let summary: String?
    public let rationale: String?
    public let taskId: String?
    public let action: String?            // what Sali proposes to do, in plain language
    public let scope: String?             // the target/scope the consent would authorize
    public let consequence: String?       // plain-language consequence (reversibility, externality)
    public let judgmentLevel: String?     // low | normal | attention | consent | high_consequence | blocked
    public let createdAt: Date?

    enum CodingKeys: String, CodingKey {
        case id, summary, rationale, action, scope, consequence
        case taskId = "task_id", judgmentLevel = "judgment_level", createdAt = "created_at"
    }

    /// The primary human-facing line for a consent card — the real backend field (`action`), falling back
    /// to `summary` if a future shape provides one.
    public var displayText: String { summary ?? action ?? "Sali is asking for your consent." }
}

// MARK: - Memory, experience, learning, capabilities (lightweight, forgiving shapes)

public struct Experience: Codable, Identifiable, Sendable {
    public var id: String { experienceId ?? UUID().uuidString }
    public let experienceId: String?
    public let summary: String?
    public let objective: String?
    public let outcome: String?
    public let confidence: Double?
    public let createdAt: Date?
    // The live ExperienceStore (src/sali/learning/experience.py: recent()/relevant_experiences()) returns
    // `content`/`evidence_state`/`procedure`/`scope` rather than `summary`/`outcome`/`confidence`/
    // `created_at` — only `provenance()` (GET /memory/{id}) fills the latter. Decoding is forgiving in both
    // directions: `summary` falls back to `content` so a card always has real text, and these extra fields
    // populate when present without requiring a second model.
    public let evidenceState: String?
    public let procedure: String?
    public let scope: String?

    enum CodingKeys: String, CodingKey {
        case experienceId = "id", summary, objective, outcome, confidence, createdAt = "created_at"
        case content, evidenceState = "evidence_state", procedure, scope
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        experienceId = try c.decodeIfPresent(String.self, forKey: .experienceId)
        objective = try c.decodeIfPresent(String.self, forKey: .objective)
        outcome = try c.decodeIfPresent(String.self, forKey: .outcome)
        confidence = try c.decodeIfPresent(Double.self, forKey: .confidence)
        createdAt = try c.decodeIfPresent(Date.self, forKey: .createdAt)
        evidenceState = try c.decodeIfPresent(String.self, forKey: .evidenceState)
        // The host sends `procedure` as a LIST of tool names (experience.py `_build_record`), not a
        // string — decoding it as String threw typeMismatch and broke every memory search in
        // production. Accept both shapes.
        if let one = try? c.decodeIfPresent(String.self, forKey: .procedure) {
            procedure = one
        } else if let many = try? c.decodeIfPresent([String].self, forKey: .procedure) {
            procedure = many.isEmpty ? nil : many.joined(separator: " → ")
        } else {
            procedure = nil
        }
        scope = try c.decodeIfPresent(String.self, forKey: .scope)
        let explicitSummary = try c.decodeIfPresent(String.self, forKey: .summary)
        let content = try c.decodeIfPresent(String.self, forKey: .content)
        summary = explicitSummary ?? content
    }

    public init(experienceId: String?, summary: String?, objective: String?, outcome: String?,
                confidence: Double?, createdAt: Date?, evidenceState: String? = nil,
                procedure: String? = nil, scope: String? = nil) {
        self.experienceId = experienceId
        self.summary = summary
        self.objective = objective
        self.outcome = outcome
        self.confidence = confidence
        self.createdAt = createdAt
        self.evidenceState = evidenceState
        self.procedure = procedure
        self.scope = scope
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encodeIfPresent(experienceId, forKey: .experienceId)
        try c.encodeIfPresent(summary, forKey: .summary)
        try c.encodeIfPresent(objective, forKey: .objective)
        try c.encodeIfPresent(outcome, forKey: .outcome)
        try c.encodeIfPresent(confidence, forKey: .confidence)
        try c.encodeIfPresent(createdAt, forKey: .createdAt)
        try c.encodeIfPresent(evidenceState, forKey: .evidenceState)
        try c.encodeIfPresent(procedure, forKey: .procedure)
        try c.encodeIfPresent(scope, forKey: .scope)
    }
}

public struct Capability: Codable, Identifiable, Sendable {
    public var id: String { capabilityId ?? name }
    public let capabilityId: String?
    public let name: String
    public let status: String?
    public let purpose: String?
    public let useCount: Int?
    public let lastUsed: Date?
    enum CodingKeys: String, CodingKey {
        case capabilityId = "id", name, status, purpose
        case useCount = "use_count", lastUsed = "last_used"
    }
}
