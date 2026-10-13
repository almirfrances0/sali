import SwiftUI

// The **Inbox** tab — the one screen that answers "what does Sali need from *me*?"
//
// It was three taps deep under "More", with no badge, no unread state, and no way to know a message
// existed without going looking for it: `NotificationStore` recorded proactive messages durably but had no
// read/unread model at all, so a message Sali sent on its own initiative had no affordance ANYWHERE in the
// app. It now has one, and the tab bar carries the count.
//
// The screen holds the two things that genuinely block Sali on you, above the record of what it has already
// told you:
//
//   1. Needs an answer — a task clarification (`waiting_for_user` on the one snapshot), a consent request
//      (`GET /api/v1/consent`), and person-directed pending questions (`GET /api/v1/pending-questions`).
//      These moved here from the old Life tab, where "what Sali needs from me" was a section inside a
//      screen about something else.
//   2. From Sali — the durable record of proactive messages, unread first-marked.
//
// Replies are always free text (§35): a reply is the signal, never a yes/no gate. Quick-reply chips, where
// offered, only fill the same field.

// MARK: - Persistence (§10/§11/§36)

/// A durable, decoupled record of a "message from Sali" so the history survives relaunch (the live event
/// ring is in-memory and bounded). Stores only the high-level, human-facing summary — never chain-of-thought.
public struct StoredNotification: Identifiable, Codable, Sendable, Equatable {
    public let id: String
    public let rawType: String
    public let summary: String
    public let origin: String?
    public let importance: String?     // agent importance, or an incident severity
    public let timestamp: Date?
    /// Which task this was about, so a row can be tapped through to its subject.
    public let taskID: String?
    /// Whether the person has actually seen it. Written by `NotificationStore`, never by a view.
    public var read: Bool

    var eventType: EventType { EventType(rawType: rawType) }
    var isAgentMessage: Bool { if case .agentMessage = eventType { return true }; return false }

    init(id: String, rawType: String, summary: String, origin: String?, importance: String?,
         timestamp: Date?, taskID: String?, read: Bool) {
        self.id = id
        self.rawType = rawType
        self.summary = summary
        self.origin = origin
        self.importance = importance
        self.timestamp = timestamp
        self.taskID = taskID
        self.read = read
    }

    enum CodingKeys: String, CodingKey {
        case id, rawType, summary, origin, importance, timestamp, taskID, read
    }

    /// Decoded by hand for exactly one reason: `read` and `taskID` did not exist in the file this store has
    /// already written. A synthesized `init(from:)` would throw on every previously-saved row and wipe the
    /// history. A row without `read` is treated as READ — it predates the feature, and turning a year of
    /// old messages into an unread badge of 300 the first time the app updates would be a lie about what
    /// is new.
    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        rawType = try container.decode(String.self, forKey: .rawType)
        summary = try container.decode(String.self, forKey: .summary)
        origin = try container.decodeIfPresent(String.self, forKey: .origin)
        importance = try container.decodeIfPresent(String.self, forKey: .importance)
        timestamp = try container.decodeIfPresent(Date.self, forKey: .timestamp)
        taskID = try container.decodeIfPresent(String.self, forKey: .taskID)
        read = try container.decodeIfPresent(Bool.self, forKey: .read) ?? true
    }
}

/// File-backed store of things Sali surfaced on its own initiative. Fed from the live event pump. The Inbox
/// tab that once read it was removed — proactive messages now land in the chat transcript — so this is kept
/// only as the durable de-dup ledger the event pump writes through. Bounded and de-duplicated by event id.
@MainActor
public final class NotificationStore: ObservableObject {
    @Published public private(set) var items: [StoredNotification] = []
    /// The number the tab bar shows. Derived from `items`, kept as stored state so reading it is free on
    /// every tab-bar render.
    @Published public private(set) var unreadCount: Int = 0

    private let maxItems = 300
    private let fileURL: URL

    public init() {
        let dir = (try? FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask,
                                                appropriateFor: nil, create: true))
            ?? FileManager.default.temporaryDirectory
        fileURL = dir.appendingPathComponent("sali-notifications.json")
        load()
    }

    /// Fold an event into durable history if it's something Sali brought to *you*.
    ///
    /// Replayed events are DELIBERATELY accepted: when the phone was closed or offline, the WebSocket
    /// replay is the ONLY delivery path for a message Sali sent on its own initiative (§8). Dropping
    /// replays here meant "Messages from Sali" silently missed everything sent while you were away.
    /// Safe because we de-duplicate by event id on the next line.
    public func record(_ event: SaliEvent) {
        guard Self.isRelevant(event.type) else { return }
        guard !items.contains(where: { $0.id == event.id }) else { return }
        let note = StoredNotification(
            id: event.id, rawType: event.rawType, summary: event.humanSummary, origin: event.origin,
            importance: event.string("importance") ?? event.string("severity"),
            timestamp: event.timestamp, taskID: event.taskId, read: false)
        items.append(note)
        if items.count > maxItems { items.removeFirst(items.count - maxItems) }
        persist()
    }

    /// Mark everything as seen. Called when the Inbox is actually looked at — never on a background event.
    public func markAllRead() {
        guard unreadCount > 0 else { return }
        for index in items.indices where !items[index].read { items[index].read = true }
        persist()
    }

    public func markRead(_ id: String) {
        guard let index = items.firstIndex(where: { $0.id == id }), !items[index].read else { return }
        items[index].read = true
        persist()
    }

    /// The ids that were unread at the moment the Inbox opened. The view keeps showing their "new" marker
    /// for the duration of the visit even after the badge clears, so opening the tab doesn't erase the
    /// answer to "which of these is new".
    public var unreadIDs: Set<String> {
        Set(items.lazy.filter { !$0.read }.map(\.id))
    }

    public func clear() {
        items.removeAll()
        persist()
    }

    static func isRelevant(_ type: EventType) -> Bool {
        switch type {
        // Almir §6: .agentMessage is DELIBERATELY EXCLUDED. Sali-initiated messages must land in
        // the chat transcript, not in a parallel Inbox surface. The chat now admits them via
        // ChatViewModel.ingest (cross-session filter permits agentMessage; the replay branch
        // handles offline-gap catch-up), so a Sali message reaches the user exactly once, in the
        // conversation where it belongs. The Inbox is reserved for things that GENUINELY need
        // Almir's attention outside the chat flow (task completion, resource incidents,
        // revocations, errors, reviewer rework verdicts).
        case .taskCompleted, .resourceIncident, .intentRevoked, .error: true
        case .other(.taskCompletionRejected): true
        default: false
        }
    }

    private func load() {
        guard let data = try? Data(contentsOf: fileURL),
              let decoded = try? JSONDecoder().decode([StoredNotification].self, from: data) else { return }
        items = decoded
        unreadCount = items.reduce(0) { $0 + ($1.read ? 0 : 1) }
    }

    /// One place that writes the file AND re-derives the count, so the badge can never disagree with the
    /// list it is counting.
    private func persist() {
        unreadCount = items.reduce(0) { $0 + ($1.read ? 0 : 1) }
        guard let data = try? JSONEncoder().encode(items) else { return }
        try? data.write(to: fileURL, options: .atomic)
    }
}
