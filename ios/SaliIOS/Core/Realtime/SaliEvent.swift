import Foundation

/// A real-time event from Sali's durable event log, as delivered over the WebSocket (live or replayed).
/// The shape is identical for both so the UI handles them uniformly (see docs/API_REFERENCE.md §6).
/// `data` is kept as a loosely-typed bag because each event type carries its own fields — the app reads only
/// the high-level, operational keys it needs and NEVER any chain-of-thought.
public struct SaliEvent: Identifiable, Sendable, Equatable {
    public let id: String
    public let type: EventType
    public let rawType: String
    public let sequence: Int?
    public let timestamp: Date?
    public let taskId: String?
    public let runId: String?
    public let origin: String?
    public let data: [String: JSONValue]
    public let replayed: Bool

    /// The high-level, human-facing description for the Activity center (never internal reasoning).
    public var humanSummary: String {
        switch type {
        case .messageStarted:   return "Sali is responding…"
        case .messageDelta:     return "Sali is responding…"
        case .messageCompleted: return "Sali finished responding"
        case .toolStarted:      return "Working: \(string("tool") ?? "a tool")"
        case .toolProgress:     return string("summary") ?? "Working…"
        case .toolCompleted:    return "Finished: \(string("tool") ?? "a step")"
        case .taskStarted:      return "Started: \(string("objective") ?? "a task")"
        case .taskProgress:     return "Step: \(string("step") ?? string("summary") ?? "in progress")"
        case .taskWaiting:      return "Waiting for your clarification"
        case .taskCompleted:    return "Task complete"
        case .taskSuspended:    return "Paused"
        case .taskResumed:      return "Resumed"
        case .researchStarted:  return "Searching documentation…"
        case .researchCompleted:return "Research complete"
        case .agentMessage:     return string("text") ?? "Sali has a message for you"
        case .resourceIncident: return "Resource incident: \(string("kind") ?? "host pressure")"
        case .resourceState:    return "System state: \(string("state") ?? "changed")"
        case .intentRevoked:    return "A task was abandoned"
        case .error:            return string("error") ?? "Something went wrong"
        case .connected, .subscribed, .pong, .other: return rawType
        }
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
}

public enum EventType: Sendable, Equatable, Hashable {
    case messageStarted, messageDelta, messageCompleted
    case toolStarted, toolProgress, toolCompleted
    case taskStarted, taskProgress, taskWaiting, taskCompleted, taskSuspended, taskResumed
    case researchStarted, researchCompleted
    case agentMessage, resourceIncident, resourceState, intentRevoked, error
    case connected, subscribed, pong, other

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
        case "agent.tool", "task.tool.started": self = .toolStarted
        case "tool.completed", "task.tool.completed": self = .toolCompleted
        // Task lifecycle — store.py emits task.* (note: 'finished' with data.status, not 'completed').
        case "task.created", "task.activated": self = .taskStarted
        case "task.progress", "task.step.completed", "task.step.failed": self = .taskProgress
        case "task.waiting_for_user": self = .taskWaiting
        case "task.finished":    self = .taskCompleted
        case "task.suspended":   self = .taskSuspended
        case "task.resumed":     self = .taskResumed
        case "research.started": self = .researchStarted
        case "research.completed": self = .researchCompleted
        // Proactive + lifecycle signals.
        case "agent.message":    self = .agentMessage      // Sali reaching out (send_agent_message)
        case "resource.incident_recorded": self = .resourceIncident
        case "intent.revoked":   self = .intentRevoked
        case "error", "agent.error": self = .error
        case "connected":        self = .connected
        case "subscribed":       self = .subscribed
        case "pong":             self = .pong
        default:                 self = .other
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
    /// Decode one WebSocket frame into a SaliEvent (best-effort; unknown frames become `.other`).
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
            origin: json["origin"] as? String,
            data: bag,
            replayed: (json["replayed"] as? Bool) ?? false
        )
    }

    private static func wrap(_ v: Any) -> JSONValue {
        switch v {
        case let s as String: .string(s)
        case let b as Bool: .bool(b)
        case let n as Double: .number(n)
        case let i as Int: .number(Double(i))
        default: .null
        }
    }

    private static func parseDate(_ v: Any?) -> Date? {
        if let d = v as? Double { return Date(timeIntervalSince1970: d) }
        if let s = v as? String { return ISO8601DateFormatter().date(from: s) }
        return nil
    }
}
