import Foundation
import UIKit

// The primary screen's brain (Prompt 13, §7). Loads durable history over REST, then assembles the live
// assistant turn purely from the WebSocket event stream — it never polls for the answer. Chain-of-thought is
// never surfaced: only `SaliEvent.humanSummary` (already high-level by construction in Core/Realtime) drives
// the `activity` state.

/// Who a bubble in the transcript belongs to. `agent` is Sali reaching out unprompted (§7 "Agent-initiated
/// messages") — never a stand-in for the model's own turn, so it stays a distinct case rather than a flag.
public enum ChatRole: String, Sendable, Equatable {
    case user, assistant, agent
}

/// A local, UI-facing chat turn. Deliberately separate from `ConversationMessage` (the wire model) so the
/// view layer never depends on backend field names, and so a still-streaming turn (no `seq`/id from the
/// server yet) can be represented the same way as a finalized one.
/// Delivery status of a locally-sent user message (nil for assistant/agent turns). Reflects delivery to
/// Sali — not Sali's response, which the streaming/thinking bubble shows. `.sending` → POST in flight;
/// `.sent` → Sali accepted it (202); `.failed` → couldn't reach Sali (tap the bubble to retry).
public enum SendStatus: String, Sendable, Equatable { case sending, sent, queued, failed }

/// A file shown in the transcript — one Sali produced (incoming, downloadable) or one you sent to Sali's
/// task workspace (outgoing). File transfer is task-scoped (§10), so both carry the task id.
public struct FileAttachment: Sendable, Equatable {
    public enum Direction: Sendable, Equatable { case incoming, outgoing }
    public let direction: Direction
    public let filename: String
    public let size: Int?
    public let taskId: String?
    public let artifactId: String?      // incoming only — identifies the downloadable artifact
    public let downloadURL: String?     // incoming only — relative path for APIClient.download
    public var sendStatus: SendStatus?  // outgoing only
    public var imageData: Data?         // outgoing image — local bytes for an inline thumbnail preview
    public var isImage: Bool { imageData != nil }

    public init(direction: Direction, filename: String, size: Int? = nil, taskId: String? = nil,
                artifactId: String? = nil, downloadURL: String? = nil, sendStatus: SendStatus? = nil,
                imageData: Data? = nil) {
        self.direction = direction; self.filename = filename; self.size = size; self.taskId = taskId
        self.artifactId = artifactId; self.downloadURL = downloadURL; self.sendStatus = sendStatus
        self.imageData = imageData
    }
}

/// One step in the live "Sali is working" trail (§7 task visibility) — a high-level, human-readable phase,
/// never chain-of-thought. Accumulated during a turn and shown as an expandable disclosure.
public struct ActivityStep: Identifiable, Sendable, Equatable {
    public let id: String
    public let label: String
    public var done: Bool
    public init(label: String, done: Bool = false) {
        self.id = UUID().uuidString; self.label = label; self.done = done
    }
}

public struct ChatMessage: Identifiable, Sendable, Equatable {
    public let id: String
    public var role: ChatRole
    public var text: String
    public let timestamp: Date
    public var isAgentInitiated: Bool
    public var sendStatus: SendStatus?
    public var attachment: FileAttachment?
    /// For agent-initiated messages: the backend `importance` (progress/update/milestone/question/
    /// warning/completion/failure), used only for a monochrome emphasis tier — never a claim of truth.
    public var agentImportance: String?
    /// The "What Sali did" trail this turn produced, carried onto the finalized bubble so the record of
    /// the work survives the end of the stream instead of being thrown away with the live state.
    public var steps: [ActivityStep] = []

    public init(id: String = UUID().uuidString, role: ChatRole, text: String, timestamp: Date = Date(),
         isAgentInitiated: Bool = false, sendStatus: SendStatus? = nil, attachment: FileAttachment? = nil,
         agentImportance: String? = nil, steps: [ActivityStep] = []) {
        self.id = id
        self.role = role
        self.text = text
        self.timestamp = timestamp
        self.isAgentInitiated = isAgentInitiated
        self.sendStatus = sendStatus
        self.attachment = attachment
        self.agentImportance = agentImportance
        self.steps = steps
    }
}

private extension ChatMessage {
    /// Map the server's conversation history (`GET /api/v1/conversation`) into local turns.
    /// Composite ID (`\(m.id)_\(m.seq)`) ensures every item in the LazyVStack is strictly unique.
    init(conversationMessage m: ConversationMessage) {
        // Role mapping: server uses "user" for Almir's messages and "assistant" for Sali's model
        // replies. "agent" would be a Sali-initiated proactive message (see agent.message event).
        // Everything not "user" is treated as an assistant bubble in chat — a plain assistant
        // bubble is the correct rendering for a proactive message today; when the backend
        // conversation history starts carrying role="agent" rows in a future turn, this maps
        // them into .agent as well, no other code change needed.
        let role: ChatRole = m.role == "user" ? .user
                            : m.role == "agent" ? .agent
                            : .assistant
        // A file Sali sent, carried durably on the history → renders as a downloadable card that
        // survives a reload (not just the live task.artifact.created card).
        let att: FileAttachment? = m.attachment.map {
            FileAttachment(direction: .incoming, filename: $0.filename, size: $0.size,
                           taskId: nil, artifactId: $0.artifactId, downloadURL: $0.downloadURL)
        }
        self.init(id: "\(m.id)_\(m.seq)",
                   role: role,
                   text: m.content,
                   timestamp: m.createdAt ?? Date(),
                   attachment: att)
    }
}

/// `POST /api/v1/conversation/message` response (§7): `{ "run_id": …, "status": "accepted" }`.
/// Decoded to validate the shape of the acceptance only. `run_id` here is minted by the HTTP layer and
/// bears no relation to the runtime run that actually produces the answer, so it is deliberately unused —
/// the real id arrives on the `agent.run` / `agent.token` events.
private struct RunAccepted: Decodable {
    let runId: String?
    let status: String
    enum CodingKeys: String, CodingKey { case runId = "run_id", status }
}

@MainActor
public final class ChatViewModel: ObservableObject {
    @Published public var messages: [ChatMessage] = []
    /// The in-progress assistant turn, assembled from `message.started` → `message.delta*` →
    /// `message.completed`. `nil` when nothing is streaming.
    @Published public var streaming: ChatMessage?
    /// True while the turn is in a REASONING phase — Sali went to a tool / another iteration
    /// AFTER streaming some intermediate text ("let me check..."). In that state we hide the
    /// visible bubble entirely and show a single "Thinking" pill; the next iteration accumulates
    /// silently, and only the FINAL iteration's text appears in the settled bubble via
    /// `messageCompleted`. Fixes the write-and-clear flash Almir called out: intermediate
    /// reasoning no longer streams into a visible bubble that then wipes on the boundary.
    /// Modeled after how Claude / ChatGPT present tool use: a chip, not intermediate prose.
    @Published public private(set) var isReasoning: Bool = false
    /// Current high-level activity label ("Thinking", "Checking files", "Running verification") or `nil`
    /// when there's nothing to report. Never chain-of-thought — always `SaliEvent.humanSummary`.
    @Published public var activity: String?
    @Published public var isSending: Bool = false
    @Published public var loadState: Loadable<Void> = .idle
    /// Bumped every time a *fresh* transcript has landed from `GET /conversation` — i.e. once per
    /// successful `load` or `reload`. The view watches it to re-place the scroll position, because
    /// "a new transcript arrived" is not something `messages.count` can express: a reload that returns
    /// the same fifty turns leaves the count untouched while replacing every row in the list.
    @Published public private(set) var historyGeneration: Int = 0
    /// A send/stream failure to surface transiently (never fabricated as an assistant reply — a failure is
    /// not a model turn, and provenance matters, per the golden rules).
    @Published public var sendError: String?
    /// The live "Sali is working" trail for the current turn — high-level phases only, expandable in the UI.
    @Published public var activitySteps: [ActivityStep] = []
    /// True when the current turn is queued behind other work (Sali is busy) — one cognition slot.
    @Published public var isQueued: Bool = false
    /// When the turn currently on screen began — the origin for the elapsed clock in the turn's status
    /// line. Set once per turn (at send, or when the first `message.started` lands for a turn we didn't
    /// start), and cleared the moment the turn is over by ANY route: completion, stop, error, revocation,
    /// timeout, or a completion replayed after a reconnect. `nil` means "nothing is running" — the clock
    /// must never keep counting for work that already finished, which is exactly the kind of decaying
    /// state this screen is not allowed to show.
    @Published public var turnStartedAt: Date?

    /// The task whose workspace chat uploads target, and whose artifacts surface inline (§10). nil when Sali
    /// has no active task — attaching is disabled then.
    @Published public var activeTaskId: String?
    @Published public var downloadingArtifactId: String?
    @Published public var downloadedFiles: [String: URL] = [:]
    /// A research report announced (research.report_ready) BEFORE the summary finishes streaming — stashed
    /// here and attached to the finalized assistant bubble as a downloadable chip. Keyed by runId so it
    /// only lands on its own turn.
    private var pendingReport: (runId: String?, attachment: FileAttachment)?
    private var seenArtifactIds: Set<String> = []
    /// Held so the WS event pump (which carries no APIClient) can fetch artifacts / active-task on task events.
    private var apiRef: APIClient?

    private let maxHistory = 200
    private var currentRunId: String?
    /// Runs the user explicitly stopped. `stop()` is otherwise a client-side lie: the backend keeps
    /// producing tokens for a beat, and those late deltas would re-open the turn in a second bubble (or
    /// land the same answer twice). Anything tagged with one of these ids is dropped on arrival.
    private var stoppedRunIds: Set<String> = []
    private var stoppedRunOrder: [String] = []
    private let maxStoppedRuns = 24
    private var timeoutTask: Task<Void, Never>?
    /// Event ids already folded into state to ensure idempotency
    private var processedEventIDs: Set<String> = []
    /// Agent-initiated (proactive) message event ids ALREADY SHOWN in chat — persisted across launches, so
    /// a reconnect REPLAY never re-appends a Sali message the user has already seen. Without this, opening
    /// the app replayed old proactive messages as if fresh (and, since they aren't in the persisted
    /// /conversation, they vanished again on the next relaunch — the "old notifications on open" bug).
    private static let seenAgentKey = "sali.chat.seenAgentMessageIDs"
    private var seenAgentOrder: [String] =
        UserDefaults.standard.stringArray(forKey: ChatViewModel.seenAgentKey) ?? []
    private lazy var seenAgentSet: Set<String> = Set(seenAgentOrder)
    /// The conversation this transcript shows. Sali's OWN task-continuation turns run in a SEPARATE
    /// work session (so its internal prompts never pollute the chat), but the WebSocket broadcasts
    /// every session's events. Without this, task chatter streamed into the transcript live and then
    /// disappeared on reload — GET /conversation is scoped to this session alone.
    private var chatSessionId: String?

    /// Remember an abandoned run so its trailing events can be discarded. Bounded — a session can stop
    /// many turns, and this must never grow without limit.
    private func markRunStopped(_ id: String?) {
        guard let id, !id.isEmpty, !stoppedRunIds.contains(id) else { return }
        stoppedRunIds.insert(id)
        stoppedRunOrder.append(id)
        while stoppedRunOrder.count > maxStoppedRuns {
            stoppedRunIds.remove(stoppedRunOrder.removeFirst())
        }
    }

    /// Streaming is coalesced: tokens accumulate here and flush into the published `streaming` bubble at
    /// ~15fps, so a long reply re-parses markdown a bounded number of times instead of once per token.
    private var streamBuffer: String = ""
    private var flushTask: Task<Void, Never>?
    private let flushIntervalNanos: UInt64 = 66_000_000

    /// Durable, on-device cache of the photos this device sent. History comes back as TEXT ONLY, so this
    /// is the ONLY place a sent picture still exists after the app is killed — see `restoreSentImages`.
    private let imageStore: SentImageStore

    public init() { imageStore = SentImageStore() }

    /// Give the view model an APIClient reference so the WS event pump (which carries no client) can fetch
    /// the active task and its artifacts when task events arrive.
    public func configure(api: APIClient) { apiRef = api }

    // MARK: - Loading history

    public func load(api: APIClient) async {
        apiRef = api
        if case .loaded = loadState, !messages.isEmpty {
            // Already loaded, avoid wiping current in-memory view
            return
        }
        loadState = .loading
        do {
            let state: ConversationState = try await api.get("conversation", query: ["limit": "50"])
            chatSessionId = state.sessionId
            messages = state.messages
                .sorted { $0.seq < $1.seq }
                .map(ChatMessage.init(conversationMessage:))
            trimHistory()
            restoreSentImages()
            loadState = .loaded(())
            historyGeneration &+= 1
        } catch {
            loadState = .failed((error as? LocalizedError)?.errorDescription ?? "Couldn't load the conversation.")
        }
    }

    public func reload(api: APIClient) async {
        do {
            let state: ConversationState = try await api.get("conversation", query: ["limit": "50"])
            chatSessionId = state.sessionId
            messages = state.messages
                .sorted { $0.seq < $1.seq }
                .map(ChatMessage.init(conversationMessage:))
            trimHistory()
            restoreSentImages()
            loadState = .loaded(())
            historyGeneration &+= 1
        } catch {
            // keep existing messages on reload failure
        }
    }

    /// Heal a frozen live turn after a WebSocket reconnect + replay catch-up (AppState bumps
    /// `replayCaughtUpTick`, which ChatView observes). Token deltas are ephemeral and are NOT replayed, so a
    /// socket drop mid-reply — a screen lock, a backgrounding, a network blip — loses the tail tokens; if the
    /// settling `agent.final` is likewise missed, the live bubble stays frozen at the last token it saw until
    /// the app is relaunched and `GET /conversation` reloads the complete reply. This does that reload WITHOUT
    /// the relaunch, and never disturbs a turn that is genuinely still streaming: the live state is torn down
    /// only once the durable transcript carries a NEWER reply than we already hold — that reply IS the settled
    /// form of the (frozen) live bubble, so this can neither duplicate a message nor wipe a real in-flight one.
    public func reconcileAfterReconnect(api: APIClient) async {
        guard case .loaded = loadState else { return }        // first load hasn't happened; .task handles it
        guard let state: ConversationState =
                try? await api.get("conversation", query: ["limit": "50"]) else { return }
        let fresh = state.messages.sorted { $0.seq < $1.seq }.map(ChatMessage.init(conversationMessage:))
        // Compare by CONTENT, never by id. A bubble finalized live is keyed by its run id, while the same
        // message loaded from REST is keyed "<dbid>_<seq>" — two different id spaces, so an id comparison
        // reads as "changed" even when nothing changed. That would let a heal fire in the MIDDLE of a
        // genuinely streaming turn and wipe the in-flight reply. The tail text is stable across both spaces.
        let oldTailText = messages.last { $0.role != .user }?.text
        let newTailText = fresh.last { $0.role != .user }?.text

        if streaming != nil || isSending {
            // (a) A FROZEN live turn: the settling agent.final never applied. Adopt the persisted reply,
            // but ONLY once the host has actually produced a NEW reply — otherwise the turn is still
            // legitimately in flight and must be left alone.
            guard let newTailText, newTailText != oldTailText else { return }
        } else {
            // (b) A turn that already SETTLED live but attachment-less (the dropped-artifact case) or short
            // (a missed-tail case): heal ONLY when the persisted tail reply carries an attachment — or more
            // text — than our in-memory copy. This is the belt-and-suspenders for the replay fix above, and
            // covers the case where the artifact event never arrived at all. If nothing is missing we return
            // WITHOUT touching `messages`, so a healthy transcript's scroll is never disturbed.
            let freshTail = fresh.last { $0.role == .assistant }
            let mineTail = messages.last { $0.role == .assistant }
            let missingAttachment = (freshTail?.attachment != nil) && (mineTail?.attachment == nil)
            let longerText = (freshTail?.text.count ?? 0) > (mineTail?.text.count ?? 0)
            guard missingAttachment || longerText else { return }
        }

        // Heal: adopt the durable transcript and tear any stale live state down.
        disarmTimeoutWatcher()
        flushTask?.cancel(); flushTask = nil
        streamBuffer = ""
        streaming = nil
        isSending = false
        isQueued = false
        isReasoning = false
        activity = nil
        activitySteps = []
        currentRunId = nil
        turnStartedAt = nil

        chatSessionId = state.sessionId
        messages = fresh
        trimHistory()
        restoreSentImages()
        historyGeneration &+= 1
    }

    // MARK: - Sending

    /// Post a turn. `suppressUserBubble` skips creating a text bubble (the caller already shows one — e.g.
    /// an image bubble that renders the caption inline). `attributeFailureTo` is the id of that caller-owned
    /// message whose *attachment* should be marked `.failed` if the POST fails, so nothing fails silently.
    public func send(_ text: String, imageRef: String? = nil,
                     initialActivity: String = "Thinking…",
                     suppressUserBubble: Bool = false, attributeFailureTo: String? = nil,
                     api: APIClient) async {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty || imageRef != nil else { return }
        // §10: one cognition slot. If a turn is already producing output, this message is DELIVERED and the
        // backend serializes it behind the current work — it must never be dropped. We keep the in-flight
        // turn's streaming state untouched and mark the new bubble `.queued`.
        let wasBusy = isSending || streaming != nil

        // Skip the text bubble when it's empty, or when the caller owns the bubble (image + inline caption).
        let outgoing: ChatMessage? = (trimmed.isEmpty || suppressUserBubble)
            ? nil
            : ChatMessage(id: UUID().uuidString, role: .user, text: trimmed, sendStatus: .sending)
        if let outgoing { messages.append(outgoing); trimHistory() }
        sendError = nil

        if !wasBusy {
            isSending = true
            activity = initialActivity   // e.g. "Looking at your image…" while the backend folds vision
            activitySteps = []
            isQueued = false
            currentRunId = nil
            // The clock starts when the user acts, not when the backend gets around to it — a queue wait
            // is part of how long this took.
            turnStartedAt = Date()
            armTimeoutWatcher()
        }

        do {
            var body: [String: Any] = ["content": trimmed]
            if let imageRef, !imageRef.isEmpty { body["image_ref"] = imageRef }  // Sali sees the image.
            let _: RunAccepted = try await api.post("conversation/message", json: body)
            if let outgoing { setSendStatus(outgoing.id, wasBusy ? .queued : .sent) }
            // NOTE: we do NOT seed `currentRunId` from the response — that id is fabricated by the HTTP
            // handler and never matches the runtime run, which would poison the reconnect-recovery guard.
            // The first `agent.run` / `agent.token` event establishes the real one.
            if !wasBusy, activity == nil { activity = "Thinking…" }
        } catch {
            // A delivery failure is surfaced ON the bubble (tap to retry), never dropped silently.
            if !wasBusy {
                disarmTimeoutWatcher()
                isSending = false
                activity = nil
                currentRunId = nil
                turnStartedAt = nil
            }
            if let outgoing { setSendStatus(outgoing.id, .failed) }
            else if let attributeFailureTo { setAttachmentStatus(attributeFailureTo, .failed) }
        }
    }

    /// As each turn settles, promote still-`queued` bubbles to `sent` — they've all been delivered, and the
    /// queue drains one at a time so this reads honestly without per-bubble run correlation.
    private func promoteQueuedToSent() {
        for i in messages.indices where messages[i].sendStatus == .queued { messages[i].sendStatus = .sent }
    }

    /// Re-send a failed message: drop the failed bubble and send its text afresh.
    public func retry(_ message: ChatMessage, api: APIClient) async {
        guard message.sendStatus == .failed, !isSending else { return }
        messages.removeAll { $0.id == message.id }
        await send(message.text, api: api)
    }

    private func setSendStatus(_ id: String, _ status: SendStatus) {
        if let i = messages.firstIndex(where: { $0.id == id }) { messages[i].sendStatus = status }
    }

    /// Append a message Sali sent on its own initiative (§10) as a distinct proactive bubble.
    private func appendAgentMessage(text: String, importance: String?, timestamp: Date?, id: String) {
        let clean = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !clean.isEmpty else { return }
        // Show a proactive Sali message at most ONCE, ever. A reconnect replays the whole gap, so without a
        // durable seen-set an old message Sali sent days ago re-appears every time the app opens.
        guard !id.isEmpty, !seenAgentSet.contains(id) else { return }
        markAgentMessageSeen(id)
        messages.append(ChatMessage(id: "\(id)_\(UUID().uuidString.prefix(6))", role: .agent, text: clean,
                                    timestamp: timestamp ?? Date(), isAgentInitiated: true,
                                    agentImportance: importance))
        trimHistory()
    }

    /// Remember (durably) that this proactive message was shown, so a later replay skips it. Bounded ring —
    /// oldest ids drop first; the set only needs to cover the reconnect-replay window, not all of history.
    private func markAgentMessageSeen(_ id: String) {
        seenAgentSet.insert(id)
        seenAgentOrder.append(id)
        if seenAgentOrder.count > 800 {
            let overflow = seenAgentOrder.count - 800
            for dropped in seenAgentOrder.prefix(overflow) { seenAgentSet.remove(dropped) }
            seenAgentOrder.removeFirst(overflow)
        }
        UserDefaults.standard.set(seenAgentOrder, forKey: Self.seenAgentKey)
    }

    // MARK: - Cancellation / Stop

    public func stop(api: APIClient) {
        disarmTimeoutWatcher()
        flushNow()   // fold buffered tokens into `streaming` before we preserve/settle it
        // Record the abandoned run BEFORE clearing it. Cancellation is asynchronous on the backend, so
        // deltas for this run can still arrive; without this they would re-open the turn in a new bubble.
        markRunStopped(currentRunId)
        markRunStopped(streaming?.id)
        isSending = false
        activity = nil
        currentRunId = nil
        turnStartedAt = nil

        // Preserve the partial reply as a finalized turn (same identity, plus this turn's activity trail).
        finalizeStreaming(finalText: nil)
        streamBuffer = ""
        activitySteps = []
        isQueued = false
        isReasoning = false   // reasoning mode is per-turn — must not leak across a stop

        // Fire the cancellation call to the backend
        Task {
            _ = try? await api.postVoid("conversation/cancel")
        }
    }

    public func reset() {
        disarmTimeoutWatcher()
        flushTask?.cancel(); flushTask = nil
        streamBuffer = ""
        messages.removeAll()
        pendingFileData.removeAll()
        // Sign-out. The photos belong to the person, not the app — they go with the session.
        imageStore.clear()
        streaming = nil
        activity = nil
        activitySteps = []
        isQueued = false
        isSending = false
        isReasoning = false
        currentRunId = nil
        turnStartedAt = nil
        processedEventIDs.removeAll()
        stoppedRunIds.removeAll()
        stoppedRunOrder.removeAll()
        loadState = .idle
    }

    // MARK: - Files & images (conversation-level, §10)

    /// Refresh which task's artifacts surface. Seeds the seen-artifact set so
    /// pre-existing files aren't re-announced as new incoming cards.
    public func refreshActiveTask(api: APIClient) async {
        apiRef = api
        guard let r: TaskListResponse = try? await api.get("tasks") else { return }
        activeTaskId = r.activeTaskId
        if let tid = r.activeTaskId { await fetchArtifacts(taskId: tid, api: api, announce: false) }
    }

    /// Fetch a task's artifacts; when `announce`, append not-yet-seen available ones as incoming file cards.
    private func fetchArtifacts(taskId: String, api: APIClient, announce: Bool) async {
        guard let list: [ArtifactMeta] = try? await api.get("tasks/\(taskId)/artifacts") else { return }
        for a in list where !seenArtifactIds.contains(a.id) {
            seenArtifactIds.insert(a.id)
            guard announce, a.available else { continue }
            let att = FileAttachment(direction: .incoming, filename: a.filename, size: a.size,
                                     taskId: taskId, artifactId: a.id, downloadURL: a.downloadURL)
            messages.append(ChatMessage(role: .assistant, text: "", attachment: att))
            trimHistory()
        }
    }

    /// Upload a file into the conversation (no task needed), show it as an outgoing card, and START A TURN
    /// about it. Uploading alone makes Sali *aware* of the drop but asks it nothing — the user would sit in
    /// front of a card that never gets a reply. `caption` is whatever was typed alongside the file; when
    /// it's empty we still post a turn that names the file, so there is always something to answer.
    public func sendFile(filename: String, data: Data, caption: String = "", api: APIClient) async {
        let att = FileAttachment(direction: .outgoing, filename: filename, size: data.count,
                                 sendStatus: .sending)
        let msg = ChatMessage(role: .user, text: caption, attachment: att)
        messages.append(msg); trimHistory()
        pendingFileData[msg.id] = data
        do {
            let ref = try await api.uploadChatFile(filename: filename, data: data)
            setAttachmentStatus(msg.id, .sent)
            // The turn carries the REAL server ref returned by the upload — never an invented path — so
            // Sali can open exactly the bytes that were stored.
            let clean = caption.trimmingCharacters(in: .whitespacesAndNewlines)
            let note = "[Attached file: \(filename) — \(ref)]"
            let content = clean.isEmpty ? note : "\(clean)\n\n\(note)"
            await send(content, initialActivity: "Reading your file…",
                       suppressUserBubble: true, attributeFailureTo: msg.id, api: api)
            // Release the retry bytes only once the TURN got through — a failed POST leaves the card
            // failed, and Retry has to be able to run the whole thing again.
            if messages.first(where: { $0.id == msg.id })?.attachment?.sendStatus != .failed {
                pendingFileData[msg.id] = nil
            }
        } catch {
            // Never silent: the card shows failed AND the reason surfaces, so the user knows what to fix.
            setAttachmentStatus(msg.id, .failed)
            sendError = (error as? LocalizedError)?.errorDescription
                ?? "Couldn't upload \(filename). Check your connection and try again."
        }
    }

    /// Re-send a failed file turn: drop the failed card and run it afresh, preserving any caption.
    public func retryFile(_ message: ChatMessage, api: APIClient) async {
        guard let att = message.attachment, !att.isImage, att.direction == .outgoing,
              att.sendStatus == .failed, let data = pendingFileData[message.id] else { return }
        let caption = message.text
        pendingFileData[message.id] = nil
        messages.removeAll { $0.id == message.id }
        await sendFile(filename: att.filename, data: data, caption: caption, api: api)
    }

    /// Send an image: upload it to the conversation workspace, show it inline, then start a turn that
    /// references it so Sali describes + reasons over it (backend folds a vision description into the turn).
    /// `caption` is any text the user typed alongside the photo; empty is fine (image-only turn).
    public func sendImage(_ data: Data, filename: String, caption: String = "", api: APIClient) async {
        // Image + caption are ONE message: the caption is stored on the bubble (`text`) so it survives a
        // retry and renders inline under the thumbnail — no separate, orphan-able caption bubble.
        let att = FileAttachment(direction: .outgoing, filename: filename, size: data.count,
                                 sendStatus: .sending, imageData: data)
        let msg = ChatMessage(role: .user, text: caption, attachment: att)
        messages.append(msg); trimHistory()
        do {
            let ref = try await api.uploadChatFile(filename: filename, data: data)
            setAttachmentStatus(msg.id, .sent)
            // Start the turn; a POST failure marks THIS image bubble failed (no silent loss, retryable).
            await send(caption, imageRef: ref, initialActivity: "Looking at your image…",
                       suppressUserBubble: true, attributeFailureTo: msg.id, api: api)
            // Keep the bytes ONLY once the turn was accepted. The server writes the durable marker for a
            // turn it received; a send that never landed leaves no marker, so it must leave no record
            // either — an orphan record would shift every later match by one on the next launch.
            // `filename` is the de-duplication key, so a retry updates its record instead of adding one.
            if messages.first(where: { $0.id == msg.id })?.attachment?.sendStatus != .failed {
                imageStore.record(filename: filename, caption: caption, data: data)
            }
        } catch {
            setAttachmentStatus(msg.id, .failed)
            sendError = (error as? LocalizedError)?.errorDescription
                ?? "Couldn't upload that photo. Check your connection and try again."
        }
    }

    /// Re-send a failed image turn: drop the failed bubble and run it afresh, preserving the caption.
    public func retryImage(_ message: ChatMessage, api: APIClient) async {
        guard let att = message.attachment, att.isImage, let data = att.imageData,
              att.sendStatus == .failed, !isSending else { return }
        let caption = message.text
        messages.removeAll { $0.id == message.id }
        await sendImage(data, filename: att.filename, caption: caption, api: api)
    }

    // MARK: - Restoring sent photos across a relaunch

    /// The PREFIX of the marker the backend writes into the durable user turn for any message that carried
    /// a photo. The backend (`_visible_user_text`) writes EITHER `[📷 Photo]` (no vision description) OR
    /// `[📷 Photo — <gist>]` when the fold produced one. Matching the whole `[📷 Photo]` literal silently
    /// missed every DESCRIBED photo — the closing bracket doesn't follow "Photo" in the gist form — so the
    /// picture was never re-attached on relaunch and showed as the raw "📷 Photo — <words>" text instead.
    /// The prefix (no closing bracket) matches both forms; it is the ONLY signal history gives us that a
    /// picture was part of a turn.
    private static let photoMarkerPrefix = "[📷 Photo"

    /// Re-attach cached photos to the restored turns that carried them.
    ///
    /// History returns text only, so a relaunch used to show the caption and the bare `[📷 Photo]` marker
    /// where the picture had been. Both sequences here are chronological — the marked turns in the
    /// transcript, and the records in `SentImageStore` — so they are matched positionally.
    ///
    /// Matched from the NEWEST end backwards, not the oldest: the transcript is the last 50 messages and
    /// the cache is the last ~40 photos, so the two windows agree about their recent end and disagree
    /// about their far end. Anything the cache no longer holds keeps its text exactly as the server sent
    /// it — a photo we cannot show is never replaced with a stand-in for one.
    private func restoreSentImages() {
        guard !imageStore.records.isEmpty else { return }

        let marked = messages.indices.filter {
            messages[$0].role == .user
                && messages[$0].attachment == nil
                && messages[$0].text.contains(Self.photoMarkerPrefix)
        }
        guard !marked.isEmpty else { return }

        let records = imageStore.records            // oldest → newest
        let pairs = min(marked.count, records.count)
        guard pairs > 0 else { return }

        for (index, record) in zip(marked.suffix(pairs), records.suffix(pairs)) {
            guard let data = imageStore.data(for: record) else { continue }
            var message = messages[index]
            message.attachment = FileAttachment(direction: .outgoing, filename: record.filename,
                                                size: record.byteCount, sendStatus: .sent, imageData: data)
            // The thumbnail IS the marker now, so the literal one stops earning its line.
            message.text = Self.captionStrippingPhotoMarker(message.text)
            messages[index] = message
        }
    }

    /// The caption without the trailing photo marker — `[📷 Photo]` OR the full `[📷 Photo — <gist>]` form.
    /// Only ever applied to a turn whose picture we actually restored (the thumbnail replaces the marker);
    /// an unmatched marker keeps its text, because it is the only remaining evidence a photo was sent.
    private static func captionStrippingPhotoMarker(_ text: String) -> String {
        guard let start = text.range(of: photoMarkerPrefix, options: .backwards) else { return text }
        // Remove from the marker's prefix through its closing bracket (covers the `— <gist>` variant);
        // if somehow unterminated, fall back to removing just the prefix so we never strip real caption.
        let close = text.range(of: "]", range: start.lowerBound..<text.endIndex)
        var caption = text
        caption.removeSubrange(start.lowerBound..<(close?.upperBound ?? start.upperBound))
        return caption.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func setAttachmentStatus(_ id: String, _ status: SendStatus) {
        if let i = messages.firstIndex(where: { $0.id == id }) { messages[i].attachment?.sendStatus = status }
    }

    /// Bytes of in-flight document uploads, kept only so a *failed* one can be retried without asking the
    /// user to re-pick the file. Images don't need this — their bytes live on the attachment already.
    private var pendingFileData: [String: Data] = [:]

    /// Download an incoming artifact into the app sandbox so a ShareLink can hand it off (reuses the
    /// artifact download path; the server-recorded host path is never requested or shown, §43).
    public func downloadAttachment(_ att: FileAttachment, api: APIClient) async {
        guard let aid = att.artifactId, let url = att.downloadURL else { return }
        downloadingArtifactId = aid
        defer { downloadingArtifactId = nil }
        do {
            let (bytes, _) = try await api.download(relativePath: url)
            let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            let fileURL = dir.appendingPathComponent(att.filename)
            try bytes.write(to: fileURL, options: .atomic)
            downloadedFiles[aid] = fileURL
        } catch {
            sendError = (error as? LocalizedError)?.errorDescription ?? "Couldn't download that file."
        }
    }

    private func refreshActiveTaskFromPump() {
        guard let api = apiRef else { return }
        Task { [weak self] in await self?.refreshActiveTask(api: api) }
    }

    private func fetchArtifactsFromPump() {
        guard let api = apiRef, let tid = activeTaskId else { return }
        Task { [weak self] in await self?.fetchArtifacts(taskId: tid, api: api, announce: true) }
    }

    // MARK: - Live event ingestion

    /// Drain every not-yet-seen event from the app's live ring, in order.
    public func ingestNew(from events: [SaliEvent]) {
        for event in events where !processedEventIDs.contains(event.id) {
            processedEventIDs.insert(event.id)
            ingest(event)
        }
        if processedEventIDs.count > 1000 {
            processedEventIDs = Set(events.map(\.id))
        }
    }

    /// The streaming state machine for exactly one event.
    public func ingest(_ event: SaliEvent) {
        // A run the user stopped is over as far as this client is concerned. The backend keeps emitting
        // for a beat after `conversation/cancel`; those tokens must not resurrect the turn or duplicate
        // the answer. Checked before anything else so no other branch can act on them.
        if event.type == .messageDelta || event.type == .messageCompleted,
           let runId = event.runId, stoppedRunIds.contains(runId) {
            return
        }

        // Real de-duplication. A reconnect can legitimately re-deliver an event we already applied (the
        // replay window overlaps live delivery), and applying an `agent.message`/`agent.final` twice would
        // double-append it. Token deltas are ephemeral (never persisted, never replayed) and high-volume,
        // so they stay OUT of the set — otherwise it would grow without bound during a long reply.
        if event.type != .messageDelta {
            guard !processedEventIDs.contains(event.id) else { return }
            processedEventIDs.insert(event.id)
            if processedEventIDs.count > 3000 {
                processedEventIDs.removeAll()
                processedEventIDs.insert(event.id)
            }
        }

        // A turn from ANOTHER session is not this conversation: Sali's autonomous task work runs in its
        // own work session and must never appear here (it would also vanish on the next reload).
        // Task/tool/system events are session-agnostic and still flow through — only the turn stream
        // is filtered.
        if let evSession = event.sessionId, let mine = chatSessionId, evSession != mine {
            switch event.type {
            case .messageStarted, .messageDelta, .messageCompleted,
                 .messageIterationBoundary:
                return
            // .agentMessage is DELIBERATELY not dropped by the cross-session filter (Almir §6):
            // a Sali-initiated message from a task-continuation session, or from a proactive
            // background stream, must still land in the chat transcript. Otherwise the message
            // survives only in the "Inbox" surface Almir wants killed.
            default:
                break
            }
        }

        // Replayed history from Postgres must not re-drive live streaming — with ONE exception: a turn
        // that COMPLETED while we were disconnected. Without this the partial reply stays frozen until the
        // 180s watchdog (and its real text is lost until a relaunch refetches the conversation). We settle
        // only OUR in-flight turn, so nothing can be duplicated into the transcript.
        if event.replayed {
            // Sali-initiated messages MUST replay into chat too (Almir §6). Otherwise a message
            // sent while the phone was offline surfaces only in the (deprecated) Inbox — which
            // is exactly the "message living only in Inbox on reconnect" defect the audit flagged.
            // Dedup by event id via seenAgentMessages; safe against multi-batch replays.
            if event.type == .agentMessage {
                appendAgentMessage(text: event.string("text") ?? "",
                                   importance: event.string("importance"),
                                   timestamp: event.timestamp, id: event.id)
                return
            }
            // A Sali-sent FILE arrives ONLY as task.artifact.created. On reconnect the turn's events REPLAY,
            // and the blanket guard below drops everything that isn't messageCompleted — so before this fix
            // the file was silently discarded on every reconnect (backgrounding, screen-lock, a token-send
            // timeout) and reappeared only after a cold relaunch fetched it over REST. Apply it here too:
            // replay delivers the artifact (seq < final) BEFORE the settling agent.final, so ingestArtifact
            // stashes it in pendingReport and finalizeStreaming then staples it onto the reply.
            if event.rawType == "task.artifact.created" {
                ingestArtifact(event)
                return
            }
            guard event.type == .messageCompleted, isSending || streaming != nil else { return }
            let matchesCurrentTurn = currentRunId == nil || event.runId == nil
                || event.runId == currentRunId || event.runId == streaming?.id
            guard matchesCurrentTurn else { return }
            disarmTimeoutWatcher()
            let finalText = event.string("text")
            if streaming == nil, let finalText, !finalText.isEmpty {
                streaming = ChatMessage(id: event.runId ?? UUID().uuidString, role: .assistant,
                                        text: finalText, timestamp: event.timestamp ?? Date())
            } else {
                flushNow()
            }
            finalizeStreaming(finalText: finalText)
            streamBuffer = ""
            isSending = false
            isQueued = false
            activity = nil
            activitySteps = []
            currentRunId = nil
            turnStartedAt = nil
            promoteQueuedToSent()
            return
        }

        // Queue signals (single-user: a queued message is almost certainly this turn's). Keyed off the raw
        // type so we needn't widen EventType. Lets the UI say "Queued — Sali is busy" instead of "Thinking".
        switch event.rawType {
        case "message.queued":
            if isSending { isQueued = true; activity = "Queued — Sali is busy" }
            return
        case "message.dequeued":
            isQueued = false
            if isSending && streaming == nil { activity = "Thinking…" }
            return
        case "agent.reengagement":
            // Sali gently resurfacing a question you left hanging — show it as a proactive message.
            appendAgentMessage(text: event.string("text") ?? event.string("question") ?? "",
                               importance: "question", timestamp: event.timestamp, id: event.id)
            return
        case "agent.follow_up_suppressed":
            return   // intentionally silent — Sali chose not to nudge

        case "execution.completed", "execution.failed":
            // AUTHORITATIVE TURN-END — the second, independent way a turn can settle.
            //
            // Settling used to hang entirely on ONE frame: `agent.final`. If that frame is lost, delayed,
            // or lands on a socket that quietly stalled, the bubble sits frozen on its last partial token
            // under a spinning "Writing…" until the app is relaunched — the exact "close and reopen to see
            // it" bug (a reply cut mid-word at "…downloa", no file card, turn already finished on the host
            // 7s earlier). The host ALSO emits this durable, replayed completion signal, so we no longer
            // depend on the streaming hop being perfect: when the turn ends we settle from the persisted
            // transcript, which always carries the COMPLETE text and the file attachment.
            //
            // No-op on the happy path: if `agent.final` already settled us, `isSending` is false and we
            // return immediately. The short grace lets a genuinely in-flight final land first so the common
            // case stays token-smooth and never double-settles.
            let matchesTurn = currentRunId == nil || event.runId == nil
                || event.runId == currentRunId || event.runId == streaming?.id
            guard isSending || streaming != nil, matchesTurn else { return }
            let api = apiRef
            Task { [weak self] in
                try? await Task.sleep(nanoseconds: 400_000_000)
                guard let self, let api, self.isSending || self.streaming != nil else { return }
                await self.reconcileAfterReconnect(api: api)
            }
            return
        case "task.artifact.created":
            ingestArtifact(event)
            return
        default:
            break
        }

        switch event.type {
        case .messageStarted:
            guard !alreadyFinalized(event.runId) else { return }
            currentRunId = event.runId ?? currentRunId
            streaming = ChatMessage(id: event.runId ?? UUID().uuidString, role: .assistant, text: "",
                                     timestamp: event.timestamp ?? Date())
            streamBuffer = ""
            isQueued = false
            isSending = true
            isReasoning = false   // fresh turn — start visible again
            activity = "Thinking…"
            // Keep the origin we already have (the send) so the clock doesn't restart when the backend
            // picks the turn up; a turn we didn't start gets its origin from the event.
            turnStartedAt = turnStartedAt ?? event.timestamp ?? Date()
            armTimeoutWatcher()

        case .messageDelta:
            let chunk = event.string("delta") ?? event.string("text") ?? ""
            guard !chunk.isEmpty else { return }
            guard !alreadyFinalized(event.runId) else { return }
            if let r = event.runId { currentRunId = r }
            // The backend now streams ONLY the final answer, as paced tokens (reasoning + "let me…"
            // narration + command chatter are NEVER sent as content — they were the write-and-erase
            // churn). So streaming straight into the bubble gives a clean live typewriter: the answer
            // types out one piece at a time, and nothing is ever erased.
            if streaming == nil {
                streaming = ChatMessage(id: event.runId ?? UUID().uuidString, role: .assistant,
                                         text: "", timestamp: event.timestamp ?? Date())
            }
            streamBuffer += chunk
            scheduleFlush()
            isSending = true
            isQueued = false
            if turnStartedAt == nil { turnStartedAt = event.timestamp ?? Date() }
            activity = nil   // real answer is flowing — drop the "Thinking…" chip
            armTimeoutWatcher()

        case .messageCompleted:
            disarmTimeoutWatcher()
            let finalText = event.string("text")
            // The final answer already streamed into the bubble as paced tokens; settle it. finalText
            // from agent.final is authoritative (used verbatim if the stream was interrupted/missed).
            if streaming == nil, let finalText, !finalText.isEmpty {
                streaming = ChatMessage(id: event.runId ?? UUID().uuidString, role: .assistant,
                                         text: finalText, timestamp: event.timestamp ?? Date())
            } else {
                flushNow()
            }
            finalizeStreaming(finalText: finalText)
            streamBuffer = ""
            isSending = false
            isQueued = false
            isReasoning = false
            activity = nil
            activitySteps = []
            currentRunId = nil
            turnStartedAt = nil
            promoteQueuedToSent()

        case .messageIterationBoundary:
            // A boundary for a run whose final has already landed cannot seal anything meaningful.
            guard !alreadyFinalized(event.runId) else { return }
            // ACT phase progressing (a tool ran). The backend streams NO content during the turn —
            // only the final answer, paced, at the very end — so nothing has been shown yet and there
            // is nothing to clear. Just keep the calm indicator; tool chips record what Sali did.
            activity = activity ?? "Thinking…"

        case .toolStarted, .toolProgress, .taskStarted, .taskProgress, .taskWaiting, .researchStarted:
            // Only THIS turn's own work belongs in the chat trail + may re-arm the watchdog. A
            // background task advancing in its own session must not appear here as noise, nor keep
            // a dead turn's spinner alive forever (finding: stuck-watchdog).
            if (isSending || streaming != nil) && eventBelongsToCurrentTurn(event) {
                let label = event.humanSummary
                activity = label
                appendActivityStep(label)
                armTimeoutWatcher()
            }
            // Learn which task is active so its files can be attached / surfaced (§10).
            if activeTaskId == nil { refreshActiveTaskFromPump() }

        case .toolCompleted, .taskCompleted, .taskSuspended, .researchCompleted:
            if (isSending || streaming != nil) && eventBelongsToCurrentTurn(event) {
                appendActivityStep(event.humanSummary, done: true)
                // The trail stays visible; the "Thinking…" chip clears once tokens flow (messageDelta).
            }
            // A step finished — a new artifact may exist; surface any as inline file cards.
            fetchArtifactsFromPump()

        case .agentMessage:
            appendAgentMessage(text: event.string("text") ?? "", importance: event.string("importance"),
                               timestamp: event.timestamp, id: event.id)

        case .researchReportReady, .fileSent:
            // A downloadable file Sali produced or SENT this turn — a research report, or any file via
            // the send_file tool (Almir is on his phone, so a local path can't reach him; this is how he
            // gets it). Attach it to THIS turn's reply as a small chip; the existing incoming-attachment
            // chip + downloadAttachment handle the tap→save. It arrives just before the reply settles, so
            // stash it keyed by runId and let finalizeStreaming place it on the bubble (also set it live
            // if the bubble already exists).
            guard let url = event.string("download_url"),
                  let rid = event.string("report_id") ?? event.string("file_id") else { break }
            let name = event.string("filename")
                ?? ((event.string("title").map { String($0.prefix(40)) } ?? "file") + ".md")
            let att = FileAttachment(direction: .incoming, filename: name, size: event.int("bytes"),
                                     taskId: nil, artifactId: rid, downloadURL: url)
            pendingReport = (event.runId, att)
            if streaming != nil { streaming?.attachment = att }

        case .error:
            // A backend revocation.py / error emit does NOT carry a session_id, so the earlier
            // cross-session guard at :617 cannot filter it. Match on runId (or streaming id)
            // instead — an error/revoke for ANOTHER run must not blow up the user's currently
            // streaming reply. Only wipe when the event belongs to THIS turn.
            if !eventBelongsToCurrentTurn(event) { break }
            disarmTimeoutWatcher()
            if streaming != nil { flushNow(); finalizeStreaming(finalText: nil) }
            streamBuffer = ""
            isSending = false
            isQueued = false
            isReasoning = false
            activity = nil
            activitySteps = []
            currentRunId = nil
            turnStartedAt = nil
            sendError = event.string("error") ?? "Something went wrong."

        case .intentRevoked:
            // Same reasoning as .error above: a background task being cancelled must not wipe
            // the user's in-progress chat state. Gate on runId/streaming-id membership.
            if !eventBelongsToCurrentTurn(event) { break }
            disarmTimeoutWatcher()
            if streaming != nil { flushNow(); finalizeStreaming(finalText: nil) }
            streamBuffer = ""
            isSending = false
            isQueued = false
            activity = nil
            activitySteps = []
            currentRunId = nil
            turnStartedAt = nil

        case .taskResumed, .resourceIncident, .resourceState,
             .connected, .subscribed, .pong, .other:
            break
        }
    }

    // MARK: - Internals

    /// True when an event plausibly belongs to the user's CURRENT chat turn. Used to keep another
    /// session's work — above all Sali's autonomous task-work session — out of this turn: it must
    /// not wipe streaming state (`.error`/`.intentRevoked`), must not pour its steps into the chat
    /// activity trail, and must not re-arm this turn's timeout watchdog (or a chat turn whose
    /// backend died would never time out while a background task keeps emitting progress).
    ///
    /// Discriminators, strongest first: a DIFFERENT session is never this turn; then a matching
    /// runId is; a present-but-mismatched runId is not; and only a truly session-less, run-less
    /// event falls back to "am I mid-turn?".
    private func eventBelongsToCurrentTurn(_ event: SaliEvent) -> Bool {
        if let evSession = event.sessionId, let mine = chatSessionId, evSession != mine {
            return false   // another session (e.g. the autonomous task-work session) — never this turn
        }
        if let evRun = event.runId {
            if let mine = currentRunId, evRun == mine { return true }
            if let streamId = streaming?.id, evRun == streamId { return true }
            return false
        }
        // No session and no runId to compare — fall back to "am I mid-turn?".
        return isSending || streaming != nil
    }

    private func armTimeoutWatcher() {
        timeoutTask?.cancel()
        timeoutTask = Task { [weak self] in
            // A STALLED turn = the reply finished on the host but the live socket never applied the settling
            // event (dropped, or filtered by session), so the UI sits on "Thinking…/Writing…" while the real
            // answer is already persisted — the "close and reopen to see it" bug. Instead of waiting the full
            // 180s, poll the durable transcript every few seconds: reconcileAfterReconnect heals the turn the
            // instant the persisted reply appears, and is a cheap no-op while it is genuinely still working.
            // Every token re-arms this watcher, so the reconcile only ever runs when NO tokens are flowing —
            // it can't fight a live stream.
            // 3s, not 7s: this is the BACKSTOP for when even the durable `execution.completed` signal
            // doesn't reach us (a quietly stalled socket). Seven seconds of staring at a half-written
            // sentence under a spinner is the thing that felt broken. Safe to poll this often now that
            // reconcile compares tail CONTENT — it can no longer misfire while a turn is still streaming.
            let step: UInt64 = 3_000_000_000
            var waited: UInt64 = 0
            while waited < 182_000_000_000 {
                try? await Task.sleep(nanoseconds: step)
                guard let self = self, !Task.isCancelled, self.isSending else { return }
                if let api = self.apiRef {
                    await self.reconcileAfterReconnect(api: api)
                    if !self.isSending { return }   // healed — the persisted reply is now on screen
                }
                waited += step
            }
            // Nothing recovered after ~3 minutes — stop pretending the turn is live.
            guard let self = self, !Task.isCancelled, self.isSending else { return }
            self.flushNow()
            self.isSending = false
            self.activity = nil
            self.turnStartedAt = nil
            if let str = self.streaming, !str.text.isEmpty {
                self.finalizeStreaming(finalText: nil)
            }
        }
    }

    private func disarmTimeoutWatcher() {
        timeoutTask?.cancel()
        timeoutTask = nil
    }

    /// Schedule a single coalesced flush of `streamBuffer` into the published bubble (~15fps).
    /// Coalesce streamed tokens into the visible bubble ~15fps so the answer types smoothly without a
    /// SwiftUI update per token. Safe from churn now: the backend only ever streams the final answer.
    private func scheduleFlush() {
        guard flushTask == nil else { return }
        flushTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: self?.flushIntervalNanos ?? 66_000_000)
            guard let self, !Task.isCancelled else { return }
            self.flushTask = nil
            if self.streaming != nil { self.streaming?.text = self.streamBuffer }
        }
    }

    /// Apply the buffer to the published bubble immediately (turn settling, stop, error, timeout).
    private func flushNow() {
        // Paint any buffered final-answer tokens into the visible bubble immediately (turn settling,
        // stop). Safe from churn: the backend only streams the final answer, so the buffer only ever
        // holds answer text, never reasoning.
        flushTask?.cancel(); flushTask = nil
        if streaming != nil { streaming?.text = streamBuffer }
    }

    /// Append a phase to the live activity trail — dedup consecutive duplicates, skip the generic
    /// "Thinking…", upgrade a repeated label to `done` when its completion event arrives, bounded length.
    private func appendActivityStep(_ label: String, done: Bool = false) {
        let clean = label.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !clean.isEmpty, clean != "Thinking…" else { return }
        if let last = activitySteps.last, last.label == clean {
            if done, !activitySteps[activitySteps.count - 1].done {
                activitySteps[activitySteps.count - 1].done = true
            }
            return
        }
        activitySteps.append(ActivityStep(label: clean, done: done))
        if activitySteps.count > 40 { activitySteps.removeFirst(activitySteps.count - 40) }
    }

    private func alreadyFinalized(_ runId: String?) -> Bool {
        // Empty must behave like nil: "".hasPrefix is true for EVERY id, so an empty run_id would
        // report the whole turn already finalized and silently drop a live reply.
        guard let runId, !runId.isEmpty else { return false }
        return messages.contains { $0.id.hasPrefix(runId) }
    }

    /// Settle the live bubble into the transcript. The finalized turn KEEPS the streaming bubble's id so
    /// SwiftUI treats it as the same view — otherwise the whole answer is torn down and rebuilt at the end
    /// of every turn. `alreadyFinalized` (not a random suffix) is what keeps it from landing twice.
    private func finalizeStreaming(finalText: String?) {
        guard var final = streaming else { return }
        // `agent.final` is AUTHORITATIVE — use it verbatim whenever it's present.
        //
        // The backend streams ONLY the complete final answer (it paces `final_text` through
        // `_pace_chunks` and then emits that SAME `final_text` as `agent.final`, and persists the same
        // variable). So the settled text equals what `GET /conversation` returns on reopen, and it is
        // never LESS complete than the live stream. The live stream, by contrast, can end a few
        // characters short: the last paced chunk may not repaint before the turn settles, or the final
        // event can arrive just ahead of the last token. That is the "thousands." → "thousand" tail-loss
        // Almir saw — visible live, whole after a reopen. Preferring `final_text` closes that gap
        // completely and guarantees the bubble matches the persisted reply. (The old rationale here —
        // keep the stream because agent.final was only the LAST iteration's text — is obsolete: the
        // backend no longer streams interleaved blocks, only the one final answer.) Only when there is
        // no final text at all (a stream that never landed one) do we keep whatever streamed.
        if let finalText, !finalText.isEmpty {
            final.text = finalText
        }
        streaming = nil
        guard !final.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        guard !alreadyFinalized(final.id) else { return }
        // The activity trail is the record of what Sali actually did — carry it onto the finished turn
        // instead of discarding it when the live state clears.
        final.steps = activitySteps
        // Attach a research report announced during this turn (stashed before the summary streamed).
        if let rep = pendingReport, rep.runId == nil || rep.runId == final.id {
            if final.attachment == nil { final.attachment = rep.attachment }
            pendingReport = nil
        }
        messages.append(final)
        trimHistory()
    }

    /// Surface a file Sali produced (`task.artifact.created`) — live OR on replay. A file the CURRENT reply
    /// sent rides ON that reply (one bubble, applied when it finalizes via `pendingReport`, or set live if
    /// the bubble is already streaming); a file from a different turn or a background task session gets its
    /// OWN card and can never ride an unrelated reply (ownership gate). Deduped by artifact_id. Reads the run
    /// id with a `data` fallback so it works identically whether the frame is live or replayed. This is the
    /// single path both the live handler and the reconnect-replay handler call — before, replay skipped it
    /// entirely, so a file sent across a socket drop was lost live and only reappeared on a cold reopen.
    private func ingestArtifact(_ event: SaliEvent) {
        guard let artifactId = event.string("artifact_id"), !seenArtifactIds.contains(artifactId),
              let filename = event.string("filename") else { return }
        seenArtifactIds.insert(artifactId)
        let att = FileAttachment(direction: .incoming, filename: filename, size: event.int("size"),
                                 taskId: event.taskId ?? activeTaskId, artifactId: artifactId,
                                 downloadURL: event.string("download_url"))
        let owningRun = event.runId ?? event.string("run_id")
        let evSession = event.sessionId
        // Belongs to the reply on screen when its run matches the current turn, OR its session matches this
        // chat, OR (legacy frames with no identity at all) we fall back to "current turn".
        let ownsCurrentTurn = (owningRun != nil && (owningRun == currentRunId || owningRun == streaming?.id))
            || (evSession != nil && evSession == chatSessionId)
            || (owningRun == nil && evSession == nil)
        if (isSending || streaming != nil) && ownsCurrentTurn {
            if streaming != nil { streaming?.attachment = att } else { pendingReport = (owningRun, att) }
        } else {
            messages.append(ChatMessage(role: .assistant, text: "", attachment: att))
            trimHistory()
        }
    }

    private func trimHistory() {
        if messages.count > maxHistory {
            messages.removeFirst(messages.count - maxHistory)
        }
        // Don't hold on to upload bytes for bubbles that no longer exist.
        if !pendingFileData.isEmpty {
            let live = Set(messages.map(\.id))
            pendingFileData = pendingFileData.filter { live.contains($0.key) }
        }
    }
}
