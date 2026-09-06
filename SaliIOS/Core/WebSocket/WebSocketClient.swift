import Combine
import Foundation

public enum ConnectionState: Equatable, Sendable, ConnectionBadgeState {
    case idle
    case connecting
    case connected
    case reconnecting(attempt: Int)
    case offline

    public var label: String {
        switch self {
        case .idle:               "Not connected"
        case .connecting:         "Connecting to Sali…"
        case .connected:          "Live"
        case .reconnecting(let n):"Reconnecting… (\(n))"
        case .offline:            "Connection interrupted"
        }
    }
    /// The same word, short enough to be set in the tracked uppercase the state family uses. `label`
    /// stays the long, spoken form — it is what VoiceOver reads and what a diagnostic line prints.
    public var shortLabel: String {
        switch self {
        case .idle:          "Offline"
        case .connecting:    "Connecting"
        case .connected:     "Live"
        case .reconnecting:  "Reconnecting"
        case .offline:       "Offline"
        }
    }

    /// The socket, drawn in the app's one state alphabet (`SaliGlyph`) rather than in a private set of
    /// dots. A live socket is the unbroken ring around nothing — the same shape presence uses for "here,
    /// and quiet" — because that is exactly what a connected, silent socket is.
    public var glyph: SaliGlyph {
        switch self {
        case .connected:                 .idle
        case .connecting, .reconnecting: .connecting
        case .idle, .offline:            .offline
        }
    }

    public var isLive: Bool { self == .connected }
}

/// The real-time WebSocket client (§25/§29/§30). It authenticates with the device access token, subscribes
/// with the last-seen sequence to recover missed events, tracks the max sequence, and reconnects with
/// backoff on drop. Events are published on a Combine subject the app consumes. It NEVER bypasses auth on
/// reconnect (the token is re-read each time, and a refresh must have already happened via APIClient).
@MainActor
public final class WebSocketClient: ObservableObject {
    @Published public private(set) var state: ConnectionState = .idle
    /// Highest durable event sequence this device has seen. PERSISTED: on a cold start an in-memory 0 makes
    /// the server replay the 500 OLDEST rows in the event table (ancient history, all of it discarded), while
    /// everything that actually happened since the app was last open is what we need. Restoring the real
    /// watermark makes reconnect-replay deliver exactly the gap.
    @Published public private(set) var lastSequence: Int = 0 {
        didSet {
            guard lastSequence > oldValue else { return }
            UserDefaults.standard.set(lastSequence, forKey: Self.lastSequenceKey)
        }
    }
    private static let lastSequenceKey = "sali.ws.lastSequence"
    /// The server caps one replay batch (MAX_REPLAY = 500). A full batch means there is very likely more.
    private static let replayBatchCap = 500
    @Published public private(set) var lastEventAt: Date?

    // MARK: - Close codes the server really uses (src/sali/api/ws.py)

    /// `4001` — unauthorized / device revoked / session expired. Retrying with the SAME credential can only
    /// fail again, so this is the one code that must break the reconnect loop instead of feeding it.
    private static let closeUnauthorized = 4001
    /// `4029` — "too many connections" (MAX_CONNECTIONS = 5). Reconnecting fast makes it strictly worse:
    /// every attempt consumes another slot on a server that is already refusing them.
    private static let closeTooManyConnections = 4029
    /// The hard back-off used when the server is refusing connections: a minute, doubling to five.
    private static let refusedBackoffFloor: Double = 60
    private static let refusedBackoffCeiling: Double = 300

    public var onTokenRefreshNeeded: (@Sendable () async -> Bool)?
    /// Called when the socket has been closed for an authorization reason AND the token could not be
    /// renewed — i.e. this device is no longer trusted. The app should return to onboarding; the client
    /// itself stops reconnecting until a genuinely different access token appears.
    public var onAuthLost: (@Sendable () -> Void)?

    private var task: URLSessionWebSocketTask?
    private let session: URLSession
    private let tokenStore: TokenStore
    private var configuration: APIConfiguration
    private var reconnectAttempt = 0
    private var pingTask: Task<Void, Never>?
    private var reconnectTask: Task<Void, Never>?
    private var explicitlyClosed = false
    /// Re-entrancy guard. `start()` is called from `onAppear` AND from the `scenePhase` change at launch, and
    /// again on every foreground — without this, two `connect()` calls in the same tick opened two sockets,
    /// every event arrived twice, and only one of them was ever cleaned up.
    private var isConnecting = false
    /// The exact access token the server rejected with 4001. While it is still the current token there is
    /// nothing to retry — reconnecting would loop forever against a permanent failure.
    private var authRejectedToken: String?
    /// Consecutive "server is refusing connections" closes, for the hard back-off.
    private var refusalStrikes = 0

    /// Live events are multicast through a Combine subject. A single-consumer `AsyncStream` was wrong here:
    /// its one iterator terminates the moment it's cancelled, so when `start()` ran again (foreground, or
    /// the onAppear + scenePhase double-call at launch) the freshly-created `for await` received NOTHING —
    /// stranding the live chat stream until a relaunch reloaded it over REST. A subject re-broadcasts to
    /// whatever is subscribed now, so reconnects and re-subscriptions keep working.
    public let eventPublisher = PassthroughSubject<SaliEvent, Never>()

    /// Fires when a replay batch has been fully delivered (the server's `subscribed` ack). Consumers use it
    /// to re-sync anything that can't be rebuilt from the event stream alone.
    public let didFinishReplay = PassthroughSubject<Void, Never>()

    public init(tokenStore: TokenStore, configuration: APIConfiguration,
                session: URLSession = .shared) {
        self.tokenStore = tokenStore
        self.configuration = configuration
        self.session = session
        // Restore the durable watermark so the first subscribe asks only for what we actually missed.
        self.lastSequence = UserDefaults.standard.integer(forKey: Self.lastSequenceKey)
    }

    public func updateConfiguration(_ cfg: APIConfiguration) { self.configuration = cfg }

    public func connect() {
        explicitlyClosed = false
        guard let token = tokenStore.accessToken, !token.isEmpty else { state = .offline; return }
        // The server already refused this exact credential and it could not be renewed. Only a genuinely
        // new token (re-enrolment, or a refresh that actually issued one) may try again.
        if let rejected = authRejectedToken, rejected == token { state = .offline; return }
        // One socket at a time (see `isConnecting`), and never a second one on top of a live one.
        guard !isConnecting else { return }
        if task != nil, state.isLive { return }
        // An explicit connect (foreground, enrolment, environment change) supersedes a pending back-off.
        reconnectTask?.cancel(); reconnectTask = nil
        openSocket()
    }

    public func disconnect() {
        explicitlyClosed = true
        reconnectTask?.cancel(); reconnectTask = nil
        pingTask?.cancel(); pingTask = nil
        isConnecting = false
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
        state = .idle
    }

    // MARK: - internals

    private func openSocket() {
        guard !explicitlyClosed else { return }
        isConnecting = true
        // Tear down anything left over before opening the new socket. Its receive/ping callbacks are
        // ignored from here on: every callback checks its own task against `self.task` first, so a
        // superseded socket can no longer drive state or schedule a reconnect of its own.
        pingTask?.cancel(); pingTask = nil
        if let previous = task {
            task = nil
            previous.cancel(with: .goingAway, reason: nil)
        }
        state = reconnectAttempt == 0 ? .connecting : .reconnecting(attempt: reconnectAttempt)
        // The credential goes in the Authorization header ONLY. The backend reads both
        // (`effective_token = header_token or token`, api/app.py) and prefers the header, but the query
        // form put a live access token into every URL the request touches — the Cloudflare access log in
        // front of this host, the tunnel's log, and the device's own CFNetwork log, which is where it was
        // found. A URL is not a secret-carrying medium; a header is.
        var req = URLRequest(url: configuration.webSocketURL)
        if let token = tokenStore.accessToken, !token.isEmpty {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        let ws = session.webSocketTask(with: req)
        task = ws
        ws.resume()
        receiveLoop(ws)
        startPing(ws)
    }

    /// Forget the replay watermark. Required when the identity or the backend changes (sign-out, or a new
    /// environment): a sequence from a different Sali/event table would silently skip real events.
    public func resetSequence() {
        lastSequence = 0
        UserDefaults.standard.removeObject(forKey: Self.lastSequenceKey)
        // A new identity/backend deserves a fresh attempt even if the previous one was refused.
        authRejectedToken = nil
        refusalStrikes = 0
    }

    /// Ask the server to replay everything after the highest sequence we've seen (0 on first connect).
    public func subscribeForRecovery() {
        send(["type": "subscribe", "after_seq": lastSequence])
    }

    public func send(_ dict: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: dict),
              let text = String(data: data, encoding: .utf8) else { return }
        task?.send(.string(text)) { _ in }
    }

    private func receiveLoop(_ ws: URLSessionWebSocketTask) {
        ws.receive { [weak self] result in
            Task { @MainActor in
                guard let self, self.task === ws else { return }   // a superseded socket never drives state
                switch result {
                case .success(let message):
                    self.handle(message)
                    if self.task === ws { self.receiveLoop(ws) }   // keep listening
                case .failure:
                    self.handleDrop(of: ws)
                }
            }
        }
    }

    private func handle(_ message: URLSessionWebSocketTask.Message) {
        let text: String
        switch message {
        case .string(let s): text = s
        case .data(let d): text = String(data: d, encoding: .utf8) ?? ""
        @unknown default: return
        }
        guard let data = text.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }

        let msgType = json["type"] as? String
        if msgType == "connected" {
            markConnected()
            // Subscribe for recovery only after connection is acknowledged
            subscribeForRecovery()
            return
        }
        if msgType == "pong" { return }
        if msgType == "subscribed" {
            // The server replays at most `replayBatchCap` events per subscribe. If it filled the batch, our
            // watermark has advanced but there is still a gap — ask again until it comes back short.
            let replayed = (json["replayed"] as? Int) ?? 0
            if replayed >= Self.replayBatchCap { subscribeForRecovery() }
            didFinishReplay.send(())
            return
        }

        guard let event = SaliEvent.decode(from: json) else { return }
        lastEventAt = Date()
        if let seq = event.sequence, seq > lastSequence { lastSequence = seq }
        if state != .connected { markConnected() }
        // control frames (subscribed) are surfaced too; consumers filter by type
        eventPublisher.send(event)
    }

    /// A frame arrived, so this socket is genuinely up: clear every failure counter.
    private func markConnected() {
        isConnecting = false
        reconnectAttempt = 0
        refusalStrikes = 0
        authRejectedToken = nil
        state = .connected
    }

    /// A socket ended. The close code is the whole point: a 4001 must not be retried like a flaky network,
    /// and a 4029 must not be retried at network speed.
    private func handleDrop(of ws: URLSessionWebSocketTask) {
        guard task === ws else { return }
        // Read the reason BEFORE dropping the task — this is the only place it exists.
        let closeCode = ws.closeCode.rawValue
        // A handshake the server refused before upgrading (Starlette closes an unauthenticated or
        // over-limit socket before `accept()`, which uvicorn turns into a plain HTTP rejection) never
        // carries a WebSocket close code — the status is the only signal there.
        let httpStatus = (ws.response as? HTTPURLResponse)?.statusCode

        task = nil
        isConnecting = false
        pingTask?.cancel(); pingTask = nil
        guard !explicitlyClosed else { return }
        // The receive failure and the ping failure both land here for the same socket; one retry is enough.
        guard reconnectTask == nil else { return }
        state = .offline

        if closeCode == Self.closeUnauthorized || httpStatus == 401 {
            // Not trusted any more. Try to renew ONCE; if the credential can't be replaced, stop.
            recoverFromRejection(stopIfUnchanged: true)
            return
        }
        if httpStatus == 403 {
            // Ambiguous by construction: a pre-accept close reaches us as 403 whether the reason was
            // "unauthorized" (4001) or "too many connections" (4029). Try a refresh once, then fall back to
            // the hard back-off rather than either hammering or giving up on a limit that clears itself.
            recoverFromRejection(stopIfUnchanged: false)
            return
        }
        reconnectAttempt += 1
        if closeCode == Self.closeTooManyConnections {
            scheduleReconnect(after: refusedBackoffDelay(), refreshingToken: false)
            return
        }
        // An ordinary drop (network, sleep, server restart): fast initial retry, then exponential backoff
        // capped at 20s.
        refusalStrikes = 0
        let delay: Double = reconnectAttempt == 1 ? 1.0 : min(pow(2.0, Double(min(reconnectAttempt, 5))), 20.0)
        scheduleReconnect(after: delay, refreshingToken: true)
    }

    /// The server refused this credential. One refresh attempt, then either reconnect with the NEW token or
    /// stop — a rejected token retried forever is exactly the loop this guards against.
    private func recoverFromRejection(stopIfUnchanged: Bool) {
        let rejected = tokenStore.accessToken
        reconnectAttempt += 1
        reconnectTask = Task { @MainActor [weak self] in
            guard let self else { return }
            var refreshed = false
            if let onRefresh = self.onTokenRefreshNeeded { refreshed = await onRefresh() }
            self.reconnectTask = nil
            // An explicit connect() (foreground, re-enrolment) may have opened a socket while the refresh
            // was in flight; it owns the connection now.
            guard !self.explicitlyClosed, !self.isConnecting else { return }
            let current = self.tokenStore.accessToken
            if refreshed, let current, !current.isEmpty, current != rejected {
                self.authRejectedToken = nil
                self.reconnectAttempt = 0
                self.openSocket()
                return
            }
            if stopIfUnchanged {
                self.authRejectedToken = rejected
                self.state = .offline
                self.onAuthLost?()
            } else {
                self.scheduleReconnect(after: self.refusedBackoffDelay(), refreshingToken: false)
            }
        }
    }

    /// A minute, doubling per consecutive refusal, capped at five — slow enough that a server refusing
    /// connections gets room to recover instead of a retry storm.
    private func refusedBackoffDelay() -> Double {
        refusalStrikes += 1
        let factor = pow(2.0, Double(min(refusalStrikes - 1, 3)))
        return min(Self.refusedBackoffFloor * factor, Self.refusedBackoffCeiling)
    }

    private func scheduleReconnect(after delay: Double, refreshingToken: Bool) {
        reconnectTask?.cancel()
        reconnectTask = Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            guard let self, !Task.isCancelled else { return }
            self.reconnectTask = nil
            guard !self.explicitlyClosed, !self.isConnecting else { return }
            if refreshingToken, let onRefresh = self.onTokenRefreshNeeded {
                _ = await onRefresh()
                guard !self.explicitlyClosed, !self.isConnecting else { return }
            }
            self.openSocket()
        }
    }

    private func startPing(_ ws: URLSessionWebSocketTask) {
        pingTask?.cancel()
        pingTask = Task { @MainActor [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 12_000_000_000) // 12s keep-alive
                guard let self, !Task.isCancelled, self.task === ws else { break }
                // Send RFC6455 native ping frame
                ws.sendPing { [weak self] error in
                    guard error != nil else { return }
                    Task { @MainActor in
                        guard let self, self.task === ws else { return }
                        self.handleDrop(of: ws)
                    }
                }
                // Send application-level text ping
                self.send(["type": "ping"])
            }
        }
    }
}
