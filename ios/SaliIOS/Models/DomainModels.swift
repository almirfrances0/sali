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

    enum CodingKeys: String, CodingKey { case id, seq, role, content, model, createdAt = "created_at" }
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

    enum CodingKeys: String, CodingKey {
        case seq, description, status, note, attempts
        case lastError = "last_error", failureClass = "failure_class", verified
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

    enum CodingKeys: String, CodingKey {
        case id, objective, status, result
        case isPrimary = "is_primary", workspaceRoot = "workspace_root", workspaceMode = "workspace_mode"
        case steps, createdAt = "created_at", updatedAt = "updated_at"
        case retryCount = "retry_count", maxRetries = "max_retries", supersededBy = "superseded_by"
    }

    public var state: TaskState { TaskState(rawValue: status) ?? .other }
    public var progress: Double {
        guard !steps.isEmpty else { return status == "done" ? 1 : 0 }
        return Double(steps.filter { $0.status == "done" }.count) / Double(steps.count)
    }
}

public enum TaskState: String, Sendable {
    case open, running, paused, waitingForUser = "waiting_for_user", blocked
    case done, failed, abandoned, cancelled, superseded, other

    public var isActive: Bool { [.open, .running, .blocked, .waitingForUser].contains(self) }
    public var isHistorical: Bool { [.done, .failed, .abandoned, .cancelled, .superseded].contains(self) }
    public var label: String {
        switch self {
        case .waitingForUser: "Waiting for you"
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
        procedure = try c.decodeIfPresent(String.self, forKey: .procedure)
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
