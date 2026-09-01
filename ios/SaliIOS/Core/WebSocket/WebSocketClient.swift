import Foundation

public enum ConnectionState: Equatable, Sendable {
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
    public var isLive: Bool { self == .connected }
}

/// The real-time WebSocket client (§25/§29/§30). It authenticates with the device access token, subscribes
/// with the last-seen sequence to recover missed events, tracks the max sequence, and reconnects with
/// backoff on drop. Events are published on an `AsyncStream` the app consumes. It NEVER bypasses auth on
/// reconnect (the token is re-read each time, and a refresh must have already happened via APIClient).
@MainActor
public final class WebSocketClient: ObservableObject {
    @Published public private(set) var state: ConnectionState = .idle
    @Published public private(set) var lastSequence: Int = 0
    @Published public private(set) var lastEventAt: Date?

    private var task: URLSessionWebSocketTask?
    private let session: URLSession
    private let tokenStore: TokenStore
    private var configuration: APIConfiguration
    private var reconnectAttempt = 0
    private var pingTimer: Timer?
    private var explicitlyClosed = false

    private var continuation: AsyncStream<SaliEvent>.Continuation?
    public let events: AsyncStream<SaliEvent>

    public init(tokenStore: TokenStore, configuration: APIConfiguration,
                session: URLSession = .shared) {
        self.tokenStore = tokenStore
        self.configuration = configuration
        self.session = session
        var cont: AsyncStream<SaliEvent>.Continuation!
        self.events = AsyncStream { cont = $0 }
        self.continuation = cont
    }

    public func updateConfiguration(_ cfg: APIConfiguration) { self.configuration = cfg }

    public func connect() {
        explicitlyClosed = false
        guard tokenStore.accessToken != nil else { state = .offline; return }
        openSocket()
    }

    public func disconnect() {
        explicitlyClosed = true
        pingTimer?.invalidate(); pingTimer = nil
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
        state = .idle
    }

    // MARK: - internals

    private func openSocket() {
        state = reconnectAttempt == 0 ? .connecting : .reconnecting(attempt: reconnectAttempt)
        var req = URLRequest(url: configuration.webSocketURL)
        if let token = tokenStore.accessToken {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        let ws = session.webSocketTask(with: req)
        task = ws
        ws.resume()
        receiveLoop()
        subscribeForRecovery()   // send after_seq immediately to replay the gap (§29)
        startPing()
    }

    /// Ask the server to replay everything after the highest sequence we've seen (0 on first connect).
    private func subscribeForRecovery() {
        send(["type": "subscribe", "after_seq": lastSequence])
    }

    private func send(_ dict: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: dict),
              let text = String(data: data, encoding: .utf8) else { return }
        task?.send(.string(text)) { _ in }
    }

    private func receiveLoop() {
        task?.receive { [weak self] result in
            guard let self else { return }
            Task { @MainActor in
                switch result {
                case .success(let message):
                    self.handle(message)
                    self.receiveLoop()   // keep listening
                case .failure:
                    self.handleDrop()
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

        if (json["type"] as? String) == "connected" {
            reconnectAttempt = 0
            state = .connected
            return
        }
        if (json["type"] as? String) == "pong" { return }

        guard let event = SaliEvent.decode(from: json) else { return }
        lastEventAt = Date()
        if let seq = event.sequence, seq > lastSequence { lastSequence = seq }
        if state != .connected { state = .connected }
        // control frames (subscribed) are surfaced too; consumers filter by type
        continuation?.yield(event)
    }

    private func handleDrop() {
        pingTimer?.invalidate(); pingTimer = nil
        guard !explicitlyClosed else { return }
        state = .offline
        reconnectAttempt += 1
        // exponential backoff, capped — survives Wi-Fi↔cellular, backgrounding, server restart (§30)
        let delay = min(pow(2.0, Double(min(reconnectAttempt, 6))), 30.0)
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            if !self.explicitlyClosed { self.openSocket() }
        }
    }

    private func startPing() {
        pingTimer?.invalidate()
        pingTimer = Timer.scheduledTimer(withTimeInterval: 25, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.send(["type": "ping"]) }
        }
    }
}
