import Foundation

// The primary screen's brain (Prompt 13, §7). Loads durable history over REST, then assembles the live
// assistant turn purely from the WebSocket event stream — it never polls for the answer. Chain-of-thought is
// never surfaced: only `SaliEvent.humanSummary` (already high-level by construction in Core/Realtime) drives
// the `activity` chip.

/// Who a bubble in the transcript belongs to. `agent` is Sali reaching out unprompted (§7 "Agent-initiated
/// messages") — never a stand-in for the model's own turn, so it stays a distinct case rather than a flag.
enum ChatRole: String, Sendable, Equatable {
    case user, assistant, agent
}

/// A local, UI-facing chat turn. Deliberately separate from `ConversationMessage` (the wire model) so the
/// view layer never depends on backend field names, and so a still-streaming turn (no `seq`/id from the
/// server yet) can be represented the same way as a finalized one.
struct ChatMessage: Identifiable, Sendable, Equatable {
    let id: String
    var role: ChatRole
    var text: String
    let timestamp: Date
    var isAgentInitiated: Bool

    init(id: String = UUID().uuidString, role: ChatRole, text: String, timestamp: Date = Date(),
         isAgentInitiated: Bool = false) {
        self.id = id
        self.role = role
        self.text = text
        self.timestamp = timestamp
        self.isAgentInitiated = isAgentInitiated
    }
}

private extension ChatMessage {
    /// Map the server's conversation history (`GET /api/v1/conversation`) into local turns. `system` rows
    /// (rare in this history) render like assistant text rather than being silently dropped — never hide
    /// data the backend actually sent.
    init(conversationMessage m: ConversationMessage) {
        self.init(id: m.id,
                   role: m.role == "user" ? .user : .assistant,
                   text: m.content,
                   timestamp: m.createdAt ?? Date())
    }
}

/// `POST /api/v1/conversation/message` response (§7): `{ "run_id": …, "status": "accepted" }`.
private struct RunAccepted: Decodable {
    let runId: String?
    let status: String
    enum CodingKeys: String, CodingKey { case runId = "run_id", status }
}

@MainActor
final class ChatViewModel: ObservableObject {
    @Published var messages: [ChatMessage] = []
    /// The in-progress assistant turn, assembled from `message.started` → `message.delta*` →
    /// `message.completed`. `nil` when nothing is streaming.
    @Published var streaming: ChatMessage?
    /// Current high-level activity label ("Thinking", "Checking files", "Running verification") or `nil`
    /// when there's nothing to report. Never chain-of-thought — always `SaliEvent.humanSummary`.
    @Published var activity: String?
    @Published var isSending: Bool = false
    @Published var loadState: Loadable<Void> = .idle
    /// A send/stream failure to surface transiently (never fabricated as an assistant reply — a failure is
    /// not a model turn, and provenance matters, per the golden rules).
    @Published var sendError: String?

    private let maxHistory = 200
    private var currentRunId: String?
    /// Event ids already folded into state. `AppState.liveEvents` is a bounded ring (§40) that can be
    /// re-delivered wholesale on every change, so re-scanning it and skipping seen ids is what makes
    /// `ingestNew` idempotent and burst-safe (several deltas can land before SwiftUI re-renders).
    private var processedEventIDs: Set<String> = []

    // MARK: - Loading history

    func load(api: APIClient) async {
        loadState = .loading
        do {
            let state: ConversationState = try await api.get("conversation", query: ["limit": "50"])
            messages = state.messages
                .sorted { $0.seq < $1.seq }
                .map(ChatMessage.init(conversationMessage:))
            trimHistory()
            loadState = .loaded(())
        } catch {
            loadState = .failed((error as? LocalizedError)?.errorDescription ?? "Couldn't load the conversation.")
        }
    }

    // MARK: - Sending

    func send(_ text: String, api: APIClient) async {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, !isSending else { return }

        // Optimistic local echo — the server doesn't echo the user's own turn back over the socket.
        messages.append(ChatMessage(role: .user, text: trimmed))
        trimHistory()
        isSending = true
        sendError = nil
        activity = "Sending…"

        do {
            let accepted: RunAccepted = try await api.post("conversation/message", json: ["content": trimmed])
            currentRunId = accepted.runId
            activity = "Thinking"
        } catch {
            isSending = false
            activity = nil
            sendError = (error as? LocalizedError)?.errorDescription ?? "Couldn't send that. Try again."
        }
    }

    // MARK: - Live event ingestion

    /// Drain every not-yet-seen event from the app's live ring, in order. Call this from a lightweight
    /// `.onChange` trigger (e.g. the latest event's id) — passing the *whole* current ring each time is
    /// intentional and cheap (bounded to a few hundred), and is what makes this robust against the ring
    /// trimming its head and against several events landing in the same SwiftUI update pass.
    func ingestNew(from events: [SaliEvent]) {
        for event in events where !processedEventIDs.contains(event.id) {
            processedEventIDs.insert(event.id)
            ingest(event)
        }
        // Bound the dedupe set too — nothing here needs unbounded memory (§40).
        if processedEventIDs.count > 1000 {
            processedEventIDs = Set(events.map(\.id))
        }
    }

    /// The streaming state machine for exactly one event.
    func ingest(_ event: SaliEvent) {
        switch event.type {
        case .messageStarted:
            guard !alreadyFinalized(event.runId) else { return }
            currentRunId = event.runId ?? currentRunId
            streaming = ChatMessage(id: event.runId ?? UUID().uuidString, role: .assistant, text: "",
                                     timestamp: event.timestamp ?? Date())
            isSending = true
            activity = "Thinking"

        case .messageDelta:
            guard matchesCurrentRun(event) else { return }
            let chunk = event.string("delta") ?? event.string("text") ?? ""
            guard !chunk.isEmpty else { return }
            if streaming == nil {
                streaming = ChatMessage(id: event.runId ?? UUID().uuidString, role: .assistant, text: "",
                                         timestamp: event.timestamp ?? Date())
            }
            streaming?.text += chunk
            // Real content is now visible in the bubble itself — the chip would be redundant noise.
            activity = nil

        case .messageCompleted:
            guard matchesCurrentRun(event) else { return }
            finalizeStreaming(finalText: event.string("text"))
            isSending = false
            activity = nil
            currentRunId = nil

        case .toolStarted, .toolProgress, .taskStarted, .taskProgress, .taskWaiting, .researchStarted:
            activity = event.humanSummary

        case .toolCompleted, .taskCompleted, .taskSuspended, .researchCompleted:
            activity = nil

        case .agentMessage:
            let text = event.string("text") ?? ""
            guard !text.isEmpty else { return }
            let message = ChatMessage(id: event.id, role: .agent, text: text,
                                       timestamp: event.timestamp ?? Date(), isAgentInitiated: true)
            guard !messages.contains(where: { $0.id == message.id }) else { return }
            messages.append(message)
            trimHistory()

        case .error:
            if streaming != nil { finalizeStreaming(finalText: nil) }
            isSending = false
            activity = nil
            sendError = event.string("error") ?? "Something went wrong."

        case .taskResumed, .intentRevoked, .resourceIncident, .resourceState,
             .connected, .subscribed, .pong, .other:
            break
        }
    }

    // MARK: - Internals

    private func alreadyFinalized(_ runId: String?) -> Bool {
        guard let runId else { return false }
        return messages.contains { $0.id == runId }
    }

    /// Guards against cross-talk from a different run's events (e.g. a stale replay after reconnect). If no
    /// run is currently tracked, the first event seen adopts its run — never silently drops data.
    private func matchesCurrentRun(_ event: SaliEvent) -> Bool {
        guard let runId = event.runId else { return true }
        guard let current = currentRunId else { currentRunId = runId; return true }
        return runId == current
    }

    private func finalizeStreaming(finalText: String?) {
        guard var final = streaming else { return }
        if let finalText, !finalText.isEmpty { final.text = finalText }
        streaming = nil
        guard !final.text.isEmpty, !messages.contains(where: { $0.id == final.id }) else { return }
        messages.append(final)
        trimHistory()
    }

    private func trimHistory() {
        if messages.count > maxHistory {
            messages.removeFirst(messages.count - maxHistory)
        }
    }
}
