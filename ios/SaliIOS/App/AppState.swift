import Foundation
import SwiftUI

/// The composition root (§3). Wires the layers once — TokenStore → APIClient → AuthService → WebSocketClient
/// — and pumps the live event stream into a bounded, observable ring that every screen can react to. Screens
/// never construct networking themselves; they receive `AppState` from the environment and call typed APIs.
@MainActor
public final class AppState: ObservableObject {
    // Configuration & auth
    @Published public var configuration: APIConfiguration
    public let tokenStore: TokenStore
    public let api: APIClient
    @Published public var auth: AuthService
    @Published public var ws: WebSocketClient

    // Live event fan-out (bounded — never unbounded history, §40)
    @Published public private(set) var liveEvents: [SaliEvent] = []
    @Published public private(set) var latestEvent: SaliEvent?
    private let maxLiveEvents = 300

    // Navigation
    @Published public var selectedTab: AppTab = .chat

    private var pumpTask: Task<Void, Never>?

    public init() {
        let store = TokenStore()
        let cfg = store.loadConfiguration()
        self.tokenStore = store
        self.configuration = cfg
        self.api = APIClient(tokenStore: store, configuration: cfg)
        self.auth = AuthService(tokenStore: store, configuration: cfg)
        self.ws = WebSocketClient(tokenStore: store, configuration: cfg)

        Task { [api, weak self] in
            await api.setOnAuthLost { [weak self] in
                Task { @MainActor in self?.auth.handleAuthLost() }
            }
        }
    }

    public var isEnrolled: Bool { auth.isEnrolled }
    public var role: Role { auth.role }

    /// Start the live connection + event pump once the device is enrolled.
    public func start() {
        guard auth.isEnrolled else { return }
        ws.connect()
        pumpTask?.cancel()
        pumpTask = Task { [weak self] in
            guard let self else { return }
            for await event in self.ws.events {
                await MainActor.run {
                    self.latestEvent = event
                    self.liveEvents.append(event)
                    if self.liveEvents.count > self.maxLiveEvents {
                        self.liveEvents.removeFirst(self.liveEvents.count - self.maxLiveEvents)
                    }
                }
            }
        }
    }

    public func stop() {
        pumpTask?.cancel(); pumpTask = nil
        ws.disconnect()
    }

    /// Apply a new environment/base URL (Settings). Rewires the layers and reconnects (§32).
    public func applyConfiguration(_ cfg: APIConfiguration) {
        configuration = cfg
        tokenStore.saveConfiguration(cfg)
        auth.updateConfiguration(cfg)
        ws.updateConfiguration(cfg)
        Task { await api.updateConfiguration(cfg) }
        if auth.isEnrolled { ws.disconnect(); ws.connect() }
    }

    /// After a successful enrollment, connect.
    public func didEnroll() { start() }

    /// Sign out locally and drop the connection.
    public func signOut() {
        stop()
        auth.signOut()
        liveEvents.removeAll()
    }
}

public enum AppTab: String, CaseIterable, Hashable {
    case chat, activity, tasks, life, system, more
}
