import Combine
import Foundation
import SwiftUI

/// The composition root (§3). Wires the layers once — TokenStore → APIClient → AuthService → WebSocketClient
/// — and pumps the live event stream into a bounded, observable ring that every screen can react to. Screens
/// never construct networking themselves; they receive `AppState` from the environment and call typed APIs.
///
/// It also owns the two things that used to have no single home:
///
/// 1. **The one "right now".** `GET /api/v1/cognitive` and `GET /api/v1/current-life` are literally the same
///    bytes (both `return await runtime.snapshot()`), and two adjacent tabs each fetched one of them and
///    rendered a *different subset* of it under the same heading. There is now exactly one fetch, one
///    decoded `SaliSnapshot`, and one card that renders it.
/// 2. **Presence.** `SaliPresence` is the app-wide answer to "what is Sali *being* right now", derived from
///    the socket, that snapshot, and the live event stream — never guessed, never decorative.
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

    // Shared Chat ViewModel so switching tabs NEVER resets or loses active streaming chat!
    @Published public var chatViewModel: ChatViewModel

    /// Durable "messages from Sali" history (survives relaunch), fed from the live event pump (§10/§11).
    public let notifications = NotificationStore()

    // MARK: - The one "right now"

    /// The single cognitive snapshot the whole app shares. Fetched here, rendered by exactly one card
    /// (`RightNowCard`), and read by `presence`. No screen fetches `/cognitive` or `/current-life` itself.
    @Published public private(set) var snapshot: Loadable<SaliSnapshot> = .idle

    /// What Sali *is* right now — the one derived presence value every surface reads.
    @Published public private(set) var presence: SaliPresence = .offline

    /// When the shared snapshot was last successfully read. Shown as provenance, never as a guess.
    @Published public private(set) var snapshotUpdatedAt: Date?

    // MARK: - Navigation

    @Published public var selectedTab: AppTab = .chat

    /// The subject a cross-tab jump was about, so the destination tab can focus it. Set by
    /// `open(_:)`; cleared once consumed. Nothing here *renders* a subject that isn't real.
    @Published public private(set) var focusedTaskID: String?

    // MARK: - Live-turn liveness (the evidence behind `.thinking` / `.working`)

    /// A reply is streaming right now: between `message.started` and `message.completed` on the live socket.
    private var liveTurnStreaming = false
    /// A tool is executing right now: between `tool.started` and `tool.completed` on the live socket.
    private var liveToolRunning = false

    /// The single, long-lived subscription to the WebSocket's event stream. Set up once and kept for the
    /// life of `AppState` — never re-created on foreground — so live events always have a live consumer.
    private var eventCancellable: AnyCancellable?
    /// Presence must move the instant the socket does, not only when an event or a snapshot arrives.
    private var connectionCancellable: AnyCancellable?
    /// The tab-bar badge is a function of unread notifications, which live in their own store.
    private var notificationsCancellable: AnyCancellable?
    /// Fires each time WebSocketClient acks a replay-batch boundary after reconnect. Consumers
    /// that need to refresh from REST after a catch-up subscribe to `replayCaughtUpTick` —
    /// the observable counter this sink bumps — so per-batch fires collapse into one work item.
    private var replayFinishedCancellable: AnyCancellable?

    /// Bumps every time the WebSocket completes a replay batch (§10, §23). Views that were stale
    /// during the offline gap observe this via `.onChange(of: appState.replayCaughtUpTick)` and
    /// refetch through their existing coalesced refresh scheduler. Deliberately an Int (not the
    /// event itself), so several batches in a row collapse into one observation.
    @Published public private(set) var replayCaughtUpTick: Int = 0

    // MODEL SWITCHER. The active chat model + its real capabilities (from Ollama), and the list the
    // owner can switch between. `modelHasVision` drives the chat image button: no vision → no image
    // attach. Defaults are permissive (vision on) until the first /models fetch; a model.changed WS
    // event updates them live. See refreshModels()/setModel(_:).
    @Published public private(set) var activeModel: String = ""
    @Published public private(set) var modelHasVision: Bool = true
    @Published public private(set) var modelHasTools: Bool = true
    @Published public private(set) var availableModels: [ModelInfo] = []
    @Published public private(set) var switchingModel: Bool = false

    /// The LAN/Remote connection manager. Discovers Sali via Bonjour, verifies it via unauth
    /// `/identity`, chooses the winning transport, and hands the URL to `applyTransport(_:runtimeId:)`
    /// — which SWAPS the endpoint without wiping the event sequence, so a LAN↔Remote hop for the
    /// same Sali runtime preserves chat replay end-to-end. See ConnectionManager.swift + DISCOVERY.md.
    public let connection: ConnectionManager

    /// In-flight snapshot read, so eight event-driven refreshes in one second are one request.
    private var snapshotTask: Task<Void, Never>?
    private var lastSnapshotAttempt: Date?

    public init() {
        let store = TokenStore()
        let cfg = store.loadConfiguration()
        self.tokenStore = store
        self.configuration = cfg
        // AuthLost is installed BEFORE any REST call can fire (previously via a stray Task that
        // could race the first refresh in start()). AppState is @MainActor, so this closure runs
        // on main and it's safe to call auth.handleAuthLost synchronously via a Task hop.
        // We use a boxed callback + late binding because AuthService itself hasn't been created
        // yet; the box is captured by both APIClient (init) and WebSocketClient (below).
        let authLostBox = _AuthLostBox()
        let client = APIClient(
            tokenStore: store, configuration: cfg,
            onAuthLost: { authLostBox.fire() }
        )
        self.api = client
        self.auth = AuthService(tokenStore: store, configuration: cfg)
        let websocket = WebSocketClient(tokenStore: store, configuration: cfg)
        websocket.onTokenRefreshNeeded = { [client] in
            await client.refreshTokens()
        }
        // Wire WebSocketClient.onAuthLost — was previously unassigned (a 4001 + failed refresh
        // would leave the socket .offline and the app never surfaced the auth loss, leading
        // users to sign-out-and-re-enroll as a "fix").
        websocket.onAuthLost = { authLostBox.fire() }
        self.ws = websocket
        self.chatViewModel = ChatViewModel()
        // ConnectionManager is initialised without `self` — it takes `applier: ConnectionApplying`
        // and we wire that up in `bindConnectionManager()` right after all stored properties are
        // set. Two-phase init avoids capturing `self` before `super.init()` semantics.
        // "Remote" is the user's OWN typed URL — not a hardcoded host. The saved configuration IS that URL,
        // so the remote fallback is simply it; LAN discovery still overrides it when Sali is on the Wi-Fi.
        self.connection = ConnectionManager(
            applier: _NullApplier.shared,
            remoteFallback: cfg
        )

        // Every stored property is initialized, so `self` is usable from here on.
        // Take the state FROM the publisher, never by reading `ws.state` back inside the sink. `@Published`
        // emits in `willSet`, so the property still holds the PREVIOUS value while this closure runs — and
        // presence derived from it lagged exactly one transition behind, permanently. The socket's last
        // transition is `.connecting → .connected`, which recomputed against `.connecting`, so presence
        // latched to `.offline` and stayed there for the whole session: a live socket, real history on
        // screen, and a header saying "Offline". Nothing recovered it, because the only other recompute is
        // driven by events and an idle Sali sends none.
        connectionCancellable = websocket.$state.sink { [weak self] newState in
            MainActor.assumeIsolated { self?.recomputePresence(connection: newState) }
        }
        notificationsCancellable = notifications.objectWillChange.sink { [weak self] _ in
            MainActor.assumeIsolated { self?.objectWillChange.send() }
        }

        // Now that `self` is usable, bind the boxed auth-lost callback to auth.handleAuthLost.
        // Any auth loss (from APIClient's refresh path or WebSocketClient's 4001 handling)
        // arrives here, on main, no scheduling race with start().
        authLostBox.handler = { [weak self] in
            Task { @MainActor in self?.auth.handleAuthLost() }
        }
        // Phase 2 of the ConnectionManager wire-up: now that all stored properties are initialised
        // we can safely retarget the manager's applier from the null stub to `self`.
        bindConnectionManager()

        // Push-tap deep-link relay (Almir §7). SaliAppDelegate writes into PushDeepLink.pending
        // on tap; we observe it here and route via the same open(subjectOf:taskID:) surface
        // that Inbox row taps already use. Foreground-suppression signals also flow through the
        // same relay (currentTab + socketLive so willPresent can suppress duplicate banners).
        pushDeepLinkCancellable = PushDeepLink.shared.$pending
            .compactMap { $0 }
            .receive(on: RunLoop.main)
            .sink { [weak self] pending in
                MainActor.assumeIsolated {
                    guard let self else { return }
                    self.handlePushTap(pending)
                    // Consume — clear so a repeat set of the same tap doesn't re-fire.
                    PushDeepLink.shared.pending = nil
                }
            }
        // Publish selectedTab into the relay so willPresent knows whether to suppress a banner.
        selectedTabCancellable = $selectedTab.sink { tab in
            Task { @MainActor in PushDeepLink.shared.currentTab = tab }
        }
        // Publish WebSocket liveness so willPresent's socketLive gate is honest.
        socketLivenessCancellable = ws.$state.sink { state in
            let isLive = (state == .connected)
            Task { @MainActor in PushDeepLink.shared.socketLive = isLive }
        }
    }

    private var pushDeepLinkCancellable: AnyCancellable?
    private var selectedTabCancellable: AnyCancellable?
    private var socketLivenessCancellable: AnyCancellable?

    private func handlePushTap(_ pending: PushDeepLink.Pending) {
        // `EventType` isn't raw-representable — it classifies the backend's real event names via
        // `init(rawType:)`, which never fails: an unrecognised name lands on `.other(.unknown)`.
        // So a missing type OR one that classifies as noise is the "unknown" case here.
        let evType = pending.eventType.map(EventType.init(rawType:))
        guard let evType, !evType.isBackgroundNoise else {
            // Unknown event type on the push — default to bringing the user to chat, which is
            // the right home for any Sali-initiated message (agent.message tap should land here).
            open(.chat)
            return
        }
        open(subjectOf: evType, taskID: pending.taskID)
    }

    /// Point the ConnectionManager's applier at THIS AppState after init has fully completed.
    private func bindConnectionManager() {
        connection.rebind(applier: self)
    }

    public var isEnrolled: Bool { auth.isLoggedIn }   // ConnectionApplying: "have credentials to connect"
    /// Whether a server URL has been entered/saved — the second half of the login gate (see RootView).
    public var hasConfiguration: Bool { tokenStore.hasConfiguration }
    public var role: Role { auth.role }


    /// Start the live connection once the device is enrolled. Safe to call repeatedly (onAppear, scenePhase
    /// active, didEnroll): the event subscription is established exactly once, and connecting again while
    /// already connected is a no-op path in the socket.
    public func start() {
        guard auth.isLoggedIn else { return }
        subscribeToEvents()
        ws.connect()
        connection.start()   // begin LAN discovery + transport reconciliation
        refreshSnapshot()
        Task { await refreshModels() }   // know the active model's vision capability up front
    }

    /// Subscribe to the WebSocket's multicast event stream exactly once. Every live event fans out into the
    /// bounded ring AND straight into the shared ChatViewModel so the transcript streams in real-time.
    private func subscribeToEvents() {
        guard eventCancellable == nil else { return }
        eventCancellable = ws.eventPublisher.sink { [weak self] event in
            // `send` is always emitted on the main actor (WebSocketClient.handle runs there), so it's safe
            // to fold the event into main-actor state synchronously — which also preserves token ordering.
            MainActor.assumeIsolated { self?.handleLiveEvent(event) }
        }
        // Reconnect-and-replay catch-up (§10 / §23). Replayed events are gated out of latestEvent
        // above, so screens driven by `.onChange(of: appState.latestEvent?.id)` would stay stale
        // after an offline gap until pull-to-refresh. Bumping this counter here — driven by
        // WebSocketClient.didFinishReplay, which fires ONCE PER BATCH — lets those screens observe
        // the catch-up and refetch through their existing coalesced scheduler. Multi-batch gaps
        // (>500 events) fire several times; the view models' 250ms settle collapses them into one.
        replayFinishedCancellable = ws.didFinishReplay.sink { [weak self] _ in
            MainActor.assumeIsolated { self?.replayCaughtUpTick &+= 1 }
        }
    }

    /// Fetch the installed models + the active one's capabilities. Sets `modelHasVision` so the chat
    /// image button is correct as soon as the app loads, before any switch.
    public func refreshModels() async {
        struct ModelsResponse: Decodable { let models: [ModelInfo]; let active: String }
        guard let resp: ModelsResponse = try? await api.get("/models") else { return }
        availableModels = resp.models
        activeModel = resp.active
        if let cur = resp.models.first(where: { $0.name == resp.active }) {
            modelHasVision = cur.vision
            modelHasTools = cur.tools
        }
    }

    /// Switch Sali's active model (owner action). The backend also broadcasts model.changed, which
    /// updates state live for every connected client; this just gives the initiating device instant
    /// feedback + surfaces errors (busy / no tools / not installed).
    public func setModel(_ name: String) async throws {
        switchingModel = true
        defer { switchingModel = false }
        struct SetResp: Decodable { let active: String; let vision: Bool; let tools: Bool }
        let resp: SetResp = try await api.post("/model", json: ["name": name])
        activeModel = resp.active
        modelHasVision = resp.vision
        modelHasTools = resp.tools
        await refreshModels()
    }

    private func handleLiveEvent(_ event: SaliEvent) {
        // MODEL SWITCHER (realtime): a switch on any device broadcasts model.changed — update the
        // active model + capabilities live so, e.g., the chat image button appears/disappears at once.
        if event.rawType == "model.changed" {
            activeModel = event.string("name") ?? activeModel
            modelHasVision = event.bool("vision") ?? modelHasVision
            modelHasTools = event.bool("tools") ?? modelHasTools
            return
        }
        // Replayed events are HISTORY being caught up after a reconnect, not things happening now. They must
        // still reach chat + notifications (that is how a turn finished offline, or a message Sali sent while
        // the app was closed, gets recovered) — but they must NOT drive "what is Sali doing right now", or a
        // cold start would render hundreds of ancient events as live activity.
        if !event.replayed {
            latestEvent = event
            liveEvents.append(event)
            if liveEvents.count > maxLiveEvents {
                liveEvents.removeFirst(liveEvents.count - maxLiveEvents)
            }
            trackLiveness(event)
            if Self.snapshotTriggers.contains(event.type) { refreshSnapshot() }
            recomputePresence()
        }
        chatViewModel.ingest(event)
        notifications.record(event)
    }

    /// The live half of presence. Only start/finish frames move these — a delta is not a state change.
    private func trackLiveness(_ event: SaliEvent) {
        switch event.type {
        case .messageStarted:
            liveTurnStreaming = true
        case .messageCompleted, .error:
            liveTurnStreaming = false
        case .toolStarted:
            liveToolRunning = true
        case .toolCompleted:
            liveToolRunning = false
        case .taskWaiting, .taskSuspended, .taskCompleted:
            liveTurnStreaming = false
            liveToolRunning = false
        default:
            break
        }
    }

    /// Events that change *what Sali is doing*, and therefore justify re-reading the one snapshot. Anything
    /// not in here is feed texture: it renders from the event itself and never costs a request (§40).
    private static let snapshotTriggers: Set<EventType> = [
        .taskStarted, .taskProgress, .taskCompleted, .taskWaiting, .taskSuspended, .taskResumed,
        .intentRevoked, .agentMessage, .resourceIncident, .messageCompleted,
    ]

    // MARK: - The one snapshot fetch

    /// Read `GET /api/v1/cognitive` once and share it. Coalesced (one in-flight request) and rate-limited
    /// (no more than one read per second) so a burst of live events can't turn into a poll.
    public func refreshSnapshot(force: Bool = false) {
        guard auth.isLoggedIn else { return }
        if let task = snapshotTask, !task.isCancelled, !force { return }
        if !force, let last = lastSnapshotAttempt, Date().timeIntervalSince(last) < 1.0 { return }
        lastSnapshotAttempt = Date()
        snapshotTask = Task { [weak self] in
            await self?.performSnapshotRead()
            self?.snapshotTask = nil
        }
    }

    /// The awaitable form, for pull-to-refresh: the gesture must not end before the data lands.
    public func refreshSnapshotAndWait() async {
        guard auth.isLoggedIn else { return }
        if let task = snapshotTask { await task.value; return }
        lastSnapshotAttempt = Date()
        let task = Task { [weak self] in
            await self?.performSnapshotRead()
            self?.snapshotTask = nil
        }
        snapshotTask = task
        await task.value
    }

    private func performSnapshotRead() async {
        let hadValue = snapshot.value != nil
        if !hadValue { snapshot = .loading }
        do {
            let value: SaliSnapshot = try await api.get("cognitive")
            snapshot = .loaded(value)
            snapshotUpdatedAt = Date()
        } catch {
            // §16: keep last-known and retry; only a genuinely first read is allowed to fail loudly.
            if !hadValue {
                snapshot = .failed((error as? APIError)?.errorDescription
                                   ?? "Couldn't read what Sali is doing.")
            }
        }
        recomputePresence()
    }

    // MARK: - Presence

    /// Derive presence from the three real sources — the socket, the shared snapshot, the live stream — in
    /// strict priority order. Live evidence outranks the snapshot (it can be a second old); what Sali needs
    /// from *you* outranks everything, because that is the only state you can do something about.
    /// - Parameter connection: the socket state to derive from. Pass it explicitly when reacting to a
    ///   `@Published` emission (see the subscription in `init`); omit it to read the current value.
    private func recomputePresence(connection: ConnectionState? = nil) {
        let next = Self.derive(connection: connection ?? ws.state,
                               snapshot: snapshot.value,
                               streaming: liveTurnStreaming,
                               toolRunning: liveToolRunning)
        if next != presence { presence = next }
    }

    static func derive(connection: ConnectionState,
                       snapshot snap: SaliSnapshot?,
                       streaming: Bool,
                       toolRunning: Bool) -> SaliPresence {
        // Not live: we genuinely do not know what Sali is doing, and saying anything else would be a lie.
        guard connection.isLive else { return .offline }
        if let snap {
            if snap.isWaitingOnYou { return .waitingOnYou }
            if snap.isBlocked { return .blocked }
        }
        if toolRunning { return .working }
        if streaming { return .thinking }
        if snap?.isWorking == true { return .working }
        return .idle
    }

    // MARK: - Cross-tab routing (§4 — nothing in the app used to be tappable through to its subject)

    /// Jump to a tab. The one programmatic entry point, so every "take me to that" affordance behaves the
    /// same way and `selectedTab` is never written from a view.
    public func open(_ tab: AppTab) {
        focusedTaskID = nil
        selectedTab = tab
    }

    /// Jump to Work, remembering which task it was about. `TasksView` owns what it does with the id; the
    /// tab switch alone already answers "show me that".
    public func openTask(_ taskID: String?) {
        focusedTaskID = taskID
        selectedTab = .work
    }

    /// Consume the pending focus (so a second visit to Work doesn't re-target an old subject).
    public func consumeFocusedTask() -> String? {
        defer { focusedTaskID = nil }
        return focusedTaskID
    }

    /// Where a live event's subject lives. Used by Inbox rows and the "right now" affordances so a tap
    /// always lands somewhere that can actually say more about it.
    public func open(subjectOf event: EventType, taskID: String?) {
        switch event {
        case .agentMessage, .messageCompleted, .messageStarted:
            open(.chat)
        case .taskStarted, .taskProgress, .taskCompleted, .taskSuspended, .taskResumed,
             .taskWaiting, .intentRevoked:
            openTask(taskID)
        case .resourceIncident, .resourceState, .error:
            open(.sali)
        default:
            taskID == nil ? open(.now) : openTask(taskID)
        }
    }

    /// Drop the live connection (e.g. on background). The event subscription is intentionally kept alive —
    /// it costs nothing while disconnected and guarantees a consumer is ready the instant we reconnect.
    public func stop() {
        ws.disconnect()
        // Keep the ConnectionManager's observers armed but stop its browser / path monitor so
        // background CPU/energy stays flat. It's restarted on the next foreground.
        connection.stop()
    }

    /// Apply a new environment/base URL (Settings) — USER-INITIATED. This is the sequence-resetting
    /// path: a Settings change could be pointing at a genuinely different Sali, whose event table
    /// is a different table, so the durable watermark from the previous one is meaningless.
    /// For automatic LAN↔Remote transport swaps on the SAME Sali runtime, use `applyTransport(_:runtimeId:)`.
    public func applyConfiguration(_ cfg: APIConfiguration) {
        configuration = cfg
        // Tell the connection manager where "remote" now IS. Without this its `remoteFallback` stays
        // whatever it was at launch — on a fresh install, the hardcoded default — and the next
        // reconcile silently swaps the app back onto that host and persists it, discarding the URL
        // just typed. Only here: this is the user-initiated path.
        connection.setRemoteFallback(cfg)
        tokenStore.saveConfiguration(cfg)
        auth.updateConfiguration(cfg)
        ws.updateConfiguration(cfg)
        Task { await api.updateConfiguration(cfg) }
        ws.resetSequence()   // a different backend has a different event table — the watermark is meaningless
        snapshot = .idle     // and a different backend's "right now" is not this one's
        snapshotUpdatedAt = nil
        // A user-initiated URL change points at (probably) a different Sali — different memory,
        // different conversations, different event table. Leaving the old transcript rendered
        // makes new events pile on top of stale bubbles from another backend, with pull-to-refresh
        // the only recovery. Reset chat + live events + notifications so ChatView.task reloads
        // against the NEW backend on next appearance.
        liveEvents.removeAll()
        chatViewModel.reset()
        if auth.isLoggedIn { ws.disconnect(); ws.connect() }
    }

    /// Swap between LAN and Remote for what the ConnectionManager verified is the SAME Sali
    /// runtime (matched `/identity` runtime_id + Bonjour TXT). Because the runtime is the same,
    /// the event table is the same, the `after_seq` watermark stays valid — replay recovers
    /// anything missed during the swap. This is the ONLY path that must not reset sequence.
    /// User-initiated URL changes (Settings) still go through `applyConfiguration(_:)`.
    public func applyTransport(_ cfg: APIConfiguration, runtimeId: String) {
        configuration = cfg
        tokenStore.saveConfiguration(cfg)
        auth.updateConfiguration(cfg)
        ws.updateConfiguration(cfg)
        Task { await api.updateConfiguration(cfg) }
        // Deliberately DO NOT call ws.resetSequence(): same Sali, same event table, same watermark.
        // Deliberately DO NOT wipe `snapshot` — same Sali's "right now" is still valid.
        if auth.isLoggedIn { ws.disconnect(); ws.connect() }
    }

    /// After a successful enrollment, connect.
    public func didEnroll() { start() }

    /// Sign out locally and drop the connection.
    public func signOut() {
        stop()
        auth.signOut()
        ws.resetSequence()
        liveEvents.removeAll()
        chatViewModel.reset()
        snapshot = .idle
        snapshotUpdatedAt = nil
        liveTurnStreaming = false
        liveToolRunning = false
        // Push binding is scoped to (deviceId, token) — a re-enroll gets a new deviceId, but APNs
        // re-issues the SAME token for the same install. Without clearing the accepted-pair here,
        // submitTokenIfNeeded would skip the POST and the new api_device row would keep an empty
        // push_token, silently dropping every server push.
        PushRegistration.shared.resetForReenrollment()
        recomputePresence()
    }

    /// The local half of a server-side factory reset: forget everything this app is still holding —
    /// transcript, live event buffer, snapshot — and rebuild the live session.
    ///
    /// Auth is deliberately UNTOUCHED: the server preserves the password and this device's session
    /// through a reset, so the phone stays signed in rather than dumping you at a login screen after
    /// you just erased everything. Resetting the WebSocket sequence is the load-bearing part — the
    /// event log the old watermark pointed into no longer exists, so replaying against it would ask
    /// for a sequence the wiped table can never satisfy.
    public func resetLocalStateAfterErase() {
        stop()
        ws.resetSequence()
        liveEvents.removeAll()
        latestEvent = nil
        chatViewModel.reset()
        // The persisted stores too, or the phone keeps describing a world the server has forgotten:
        // banners about deleted tasks, dedup ids for deleted messages (which would swallow a new
        // message reusing an id), and a "last looked at" stamp per task id that no longer exists.
        // An erase that leaves these behind is not a reset, it is a half-reset that only looks clean.
        chatViewModel.purgePersistedState()
        notifications.clear()
        TaskSeenStore.purge()
        snapshot = .idle
        snapshotUpdatedAt = nil
        liveTurnStreaming = false
        liveToolRunning = false
        recomputePresence()
        start()
    }
}

// MARK: - ConnectionApplying (LAN discovery / transport failover)

/// AppState exposes the seam ConnectionManager needs to swap transports (without touching the
/// event sequence) OR fully re-swap to a genuinely different Sali (which MUST reset it). Kept
/// as an extension so the discovery layer's contract is one file to read.
extension AppState: ConnectionApplying {
    public var currentConfiguration: APIConfiguration { configuration }
    public var webSocketStatePublisher: AnyPublisher<ConnectionState, Never> {
        ws.$state.eraseToAnyPublisher()
    }
    public func applyConfigurationFullReset(_ cfg: APIConfiguration) {
        // Route through the existing user-initiated path — it resets sequence, wipes snapshot,
        // reconnects. Used by ConnectionManager if a runtime_id CHANGE is detected on what it
        // thought was the same peer (which means we're actually talking to a different Sali
        // now, so replay against the previous event table would be nonsense).
        applyConfiguration(cfg)
    }
    public var enrolledRuntimeId: String? { tokenStore.enrolledRuntimeId }
    public func setEnrolledRuntimeId(_ id: String?) { tokenStore.enrolledRuntimeId = id }
    public var forcedRemote: Bool { tokenStore.forcedRemote }
    public func setForcedRemote(_ v: Bool) { tokenStore.forcedRemote = v }
}

/// Late-bindable box for the auth-lost callback. AppState hands one instance to both APIClient
/// (via init, so it's set before any REST call) and WebSocketClient (via property assignment).
/// The actual handler is installed at the END of AppState.init once `self` is usable, so both
/// consumers pick up the real routing to `auth.handleAuthLost()` — no unordered Task race.
final class _AuthLostBox: @unchecked Sendable {
    var handler: (@Sendable () -> Void)?
    func fire() { handler?() }
}

/// Placeholder applier used only during AppState's first init pass (before `self` is available).
/// The real applier is installed by `bindConnectionManager()` a few lines later. Its methods are
/// no-ops so a stray call in that microscopic window can't crash the app.
@MainActor
final class _NullApplier: ConnectionApplying {
    static let shared = _NullApplier()
    private let subject = CurrentValueSubject<ConnectionState, Never>(.idle)
    var currentConfiguration: APIConfiguration { APIConfiguration(environment: .production) }
    var isEnrolled: Bool { false }
    var webSocketStatePublisher: AnyPublisher<ConnectionState, Never> { subject.eraseToAnyPublisher() }
    func applyTransport(_ configuration: APIConfiguration, runtimeId: String) { /* no-op */ }
    func applyConfigurationFullReset(_ configuration: APIConfiguration) { /* no-op */ }
    var enrolledRuntimeId: String? { nil }
    func setEnrolledRuntimeId(_ id: String?) { /* no-op */ }
    var forcedRemote: Bool { false }
    func setForcedRemote(_ v: Bool) { /* no-op */ }
}

// MARK: - Tabs

/// Four tabs, each answering exactly ONE question.
///
/// - `chat`  — talk to Sali (and where Sali talks to you: questions, consent and proactive messages arrive
///             here as normal messages, answered by a plain reply — no separate inbox to visit).
/// - `now`   — what is Sali doing, and what did it do?
/// - `work`  — the tasks.
/// - `sali`  — everything about Sali itself: agenda, memory, learning, schedules, system, settings.
///
/// The Inbox tab was removed: anything Sali needs from Almir reaches him as a chat message (the runtime
/// resolves a pending question/consent from a free-text reply), so a dedicated surface only duplicated it.
public enum AppTab: String, CaseIterable, Hashable, Sendable {
    case chat, now, work, sali

    public var title: String {
        switch self {
        case .chat:  "Chat"
        case .now:   "Now"
        case .work:  "Work"
        case .sali:  "Sali"
        }
    }

    /// The glyph, in BOTH states. One family by construction: simple, single-object outlines — a
    /// bubble, a clock, a checked square, a ring around a core — drawn at one weight. The old set
    /// mixed a two-object bubble pair, a medical ECG trace, a tray, a checklist and a ring: five
    /// different drawing styles, which is why the bar read as stock.
    ///
    /// There is no filled counterpart any more. Selection used to swap in `.fill`, which made the
    /// selected tab a block of ink among four line drawings — the "bold icons" Almir asked to lose.
    /// Selection is now carried entirely by ink tier and label weight (`SaliTabBarStyle`), so the
    /// drawing itself never changes and the eye tracks one constant shape.
    public var symbol: String {
        switch self {
        case .chat:  "bubble.left"
        case .now:   "clock"
        case .work:  "checkmark.square"
        // The ring-around-a-core echoes `SaliMark`, which is the identity the design system carries.
        case .sali:  "circle.circle"
        }
    }

}

// MARK: - Presence

/// What Sali *is* right now, in one value the whole app can read.
///
/// Six states, each with exactly one derivation and no overlap. Nothing here is a mood or a guess: every
/// case is the conclusion of a real signal, and `.offline` is the honest answer whenever the socket cannot
/// tell us (a stale snapshot must never be rendered as a live Sali).
public enum SaliPresence: String, Sendable, Equatable, CaseIterable {
    /// The socket is not live. We do not know what Sali is doing, and we say so.
    case offline
    /// Connected, nothing running: no task, no activity, no stream.
    case idle
    /// A reply is streaming right now (`message.started` seen, `message.completed` not yet).
    case thinking
    /// A tool is executing, or the snapshot says a task/activity is running.
    case working
    /// Sali is blocked on *you*: a task clarification, a consent request, or `life_mode == "waiting"`.
    case waitingOnYou
    /// Sali stopped itself safely — a blocked activity, or a paused/blocked/interrupted task.
    case blocked

    /// The short label the presence indicator shows. Deliberately about Sali, not about a socket — and
    /// deliberately the *derivation*, not a mood: `.thinking` is set by `message.started`, so the honest
    /// word for it is "Replying", not the unfalsifiable "Thinking".
    public var label: String {
        switch self {
        case .offline:      "Offline"
        case .idle:         "Idle"
        case .thinking:     "Replying"
        case .working:      "Working"
        case .waitingOnYou: "Needs you"
        case .blocked:      "Paused"
        }
    }

    /// One line of provenance — what this state was actually derived from.
    public var explanation: String {
        switch self {
        case .offline:      "Not connected — this is the last thing Sali told us, not what it is doing now."
        case .idle:         "Connected. No task, activity, or reply in progress."
        case .thinking:     "A reply is streaming right now."
        case .working:      "A tool or task is running right now."
        case .waitingOnYou: "Sali is blocked until you answer."
        case .blocked:      "Sali stopped itself rather than guess."
        }
    }

    /// The shape this state is drawn as — the app's one state alphabet (`SaliGlyph`), shared with the
    /// chat header and the connection row so a state that means the same thing looks the same everywhere.
    /// Presence uses six of the eight letters; `connecting` and `queued` belong to the two surfaces that
    /// can see a socket handshake and a queued turn, and presence deliberately cannot.
    public var glyph: SaliGlyph {
        switch self {
        case .offline:      .offline
        case .idle:         .idle
        case .thinking:     .replying
        case .working:      .working
        case .waitingOnYou: .needsYou
        case .blocked:      .paused
        }
    }

    /// Whether this state is asking something of the person. Drives the one badge that may raise its voice.
    public var needsYou: Bool { self == .waitingOnYou }

    /// Whether Sali is doing something under its own power right now.
    public var isBusy: Bool { self == .working || self == .thinking }

    /// Where a tap on the presence badge should land: the screen that can actually say more.
    public var destination: AppTab {
        switch self {
        case .waitingOnYou: .chat   // Sali's question is in the transcript; a reply there answers it
        default:            .now
        }
    }
}

// MARK: - The one snapshot

/// `GET /api/v1/cognitive` — which is byte-for-byte `GET /api/v1/current-life`: both routes are
/// `return await runtime.snapshot()`, i.e. `CognitiveState.snapshot()`
/// (src/sali/api/routes/api.py:639 and :879; src/sali/runtime/cognitive.py:134).
///
/// This is the UNION of the fields the two screens used to render separately, decoded once. Every field is
/// optional: a key the backend stops sending degrades to nil rather than showing a guess, and — critically
/// — `next_wakeup` is kept as a String and parsed leniently, because Python's `datetime.isoformat()` emits
/// a timezone-naive stamp that `ISO8601DateFormatter` rejects, which would otherwise fail the whole decode
/// and blank a screen over one date.
/// One installed model the owner can switch Sali to, with the capabilities Ollama reports. `vision`
/// gates the chat image button; `tools` is required (the backend refuses a non-tool model).
public struct ModelInfo: Decodable, Equatable, Sendable, Identifiable {
    public var id: String { name }
    public let name: String
    public let size: Int?
    public let family: String?
    public let parameterSize: String?
    public let quantization: String?
    public let vision: Bool
    public let tools: Bool
    public let active: Bool

    enum CodingKeys: String, CodingKey {
        case name, size, family, vision, tools, active
        case parameterSize = "parameter_size"
        case quantization
    }
}


public struct SaliSnapshot: Decodable, Equatable, Sendable {
    // Identity of the work
    public let taskID: String?
    public let objective: String?
    public let taskStatus: String?
    public let workspace: String?
    public let phase: String?
    public let nextAction: String?
    public let currentStep: Int?
    public let activeTool: String?
    public let foregroundBusy: Bool?
    public let researchCount: Int?
    public let reviewStatus: String?

    // Life state
    public let lifeMode: String?
    public let currentActivity: SaliActivity?
    public let blockedReason: String?
    public let waitingReason: String?
    public let waitingForUser: SaliClarification?
    public let pendingConsent: SaliConsentAsk?
    private let nextWakeupRaw: String?

    // Durable intent — the counts behind the agenda (the lists come from /goals, /initiatives, …)
    public let activeGoals: Int?
    public let openInitiatives: Int?
    public let openObligations: Int?
    public let commitments: Int?
    public let openCommitments: Int?
    public let digitalObjects: Int?
    public let routinesDue: Int?
    public let pendingQuestions: Int?
    public let openConversations: Int?
    public let openResourceIncidents: Int?
    public let pendingBehaviorProposals: Int?

    enum CodingKeys: String, CodingKey {
        case taskID = "task_id"
        case objective
        case taskStatus = "task_status"
        case workspace
        case phase
        case nextAction = "next_action"
        case currentStep = "current_step"
        case activeTool = "active_tool"
        case foregroundBusy = "foreground_busy"
        case researchCount = "research_count"
        case reviewStatus = "review_status"
        case lifeMode = "life_mode"
        case currentActivity = "current_activity"
        case blockedReason = "blocked_reason"
        case waitingReason = "waiting_reason"
        case waitingForUser = "waiting_for_user"
        case pendingConsent = "pending_consent"
        case nextWakeupRaw = "next_wakeup"
        case activeGoals = "active_goals"
        case openInitiatives = "open_initiatives"
        case openObligations = "open_obligations"
        case commitments
        case openCommitments = "open_commitments"
        case digitalObjects = "digital_objects"
        case routinesDue = "routines_due"
        case pendingQuestions = "pending_questions"
        case openConversations = "open_conversations"
        case openResourceIncidents = "open_resource_incidents"
        case pendingBehaviorProposals = "pending_behavior_proposals"
    }

    /// When Sali next wakes itself up. Parsed leniently — see the type's doc comment.
    public var nextWakeup: Date? { SaliISO8601.parse(nextWakeupRaw) }

    // MARK: Derived reads (the ONLY place these rules live, so presence and the card can never disagree)

    /// Sali is blocked on the person: a task clarification, a consent ask, or the server's own verdict.
    public var isWaitingOnYou: Bool {
        waitingForUser != nil || pendingConsent != nil || lifeMode == "waiting"
            || taskStatus == "waiting_for_user" || taskStatus == "waiting"
    }

    /// Sali stopped itself. `blocked_reason` is only ever set from a blocked activity's own error.
    public var isBlocked: Bool {
        (blockedReason?.isEmpty == false) || lifeMode == "interrupted"
            || taskStatus == "paused" || taskStatus == "blocked"
    }

    /// Sali is doing something under its own power.
    public var isWorking: Bool {
        lifeMode == "working" || foregroundBusy == true || currentActivity != nil
            || taskStatus == "running"
    }

    /// The single most honest headline for "what is Sali doing" — the live activity if there is one, else
    /// the task objective. Never both, never a placeholder.
    public var headline: String? {
        if let described = currentActivity?.displayText { return described }
        if let objective, !objective.isEmpty { return objective }
        return nil
    }

    /// Why the headline is what it is, when the headline came from an activity that has an objective behind
    /// it. Nil when it would just repeat the headline.
    public var headlineContext: String? {
        guard currentActivity?.displayText != nil, let objective, !objective.isEmpty else { return nil }
        return objective
    }

    /// The reason Sali is waiting or paused, in the person's words — whichever one actually applies.
    public var attentionReason: String? {
        if let reason = waitingReason, !reason.isEmpty { return reason }
        if let question = waitingForUser?.question, !question.isEmpty { return question }
        if let ask = pendingConsent?.displayText { return ask }
        if let blocked = blockedReason, !blocked.isEmpty { return blocked }
        return nil
    }

    /// Everything durable Sali is carrying, as one number. Zero is a real answer, not an empty state.
    public var durableIntentCount: Int {
        (activeGoals ?? 0) + (openInitiatives ?? 0) + (openObligations ?? 0) + (commitments ?? 0)
    }
}

/// `current_activity` from `_life_snapshot` — `{kind, status, description, error}`
/// (src/sali/runtime/cognitive.py:357).
public struct SaliActivity: Decodable, Equatable, Sendable {
    public let kind: String?
    public let status: String?
    public let description: String?
    public let error: String?

    /// The description if the store recorded one, else the activity kind made readable. Never "Working".
    public var displayText: String? {
        if let description, !description.isEmpty { return description }
        guard let kind, !kind.isEmpty else { return nil }
        return kind.replacingOccurrences(of: "_", with: " ").capitalized
    }
}

/// `waiting_for_user` — `{id, question}` from `task_question` (src/sali/runtime/cognitive.py:403).
public struct SaliClarification: Decodable, Equatable, Sendable, Identifiable {
    public let id: String?
    public let question: String?
}

/// `pending_consent` — `{id, action, scope, consequence, judgment_level}`
/// (src/sali/tasks/consent.py:81).
public struct SaliConsentAsk: Decodable, Equatable, Sendable, Identifiable {
    public let id: String?
    public let action: String?
    public let scope: String?
    public let consequence: String?
    public let judgmentLevel: String?

    enum CodingKeys: String, CodingKey {
        case id, action, scope, consequence
        case judgmentLevel = "judgment_level"
    }

    public var displayText: String? {
        guard let action, !action.isEmpty else { return nil }
        return action
    }
}

// MARK: - Lenient timestamps

/// Python's `datetime.isoformat()` emits a timezone-naive stamp for a naive datetime, and
/// `ISO8601DateFormatter` refuses those outright. Decoding such a field as `Date` throws, which fails the
/// WHOLE response — one date blanking an entire screen. Every optional backend timestamp this feature
/// touches is decoded as a String and parsed here instead: unparseable simply means "no date".
public enum SaliISO8601 {
    public static func parse(_ raw: String?) -> Date? {
        guard let raw, !raw.isEmpty else { return nil }
        let iso = ISO8601DateFormatter()
        iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = iso.date(from: raw) { return date }
        iso.formatOptions = [.withInternetDateTime]
        if let date = iso.date(from: raw) { return date }
        for format in ["yyyy-MM-dd'T'HH:mm:ss.SSSSSS", "yyyy-MM-dd'T'HH:mm:ss.SSS",
                       "yyyy-MM-dd'T'HH:mm:ss"] {
            let formatter = DateFormatter()
            formatter.locale = Locale(identifier: "en_US_POSIX")
            formatter.timeZone = TimeZone(identifier: "UTC")
            formatter.dateFormat = format
            if let date = formatter.date(from: raw) { return date }
        }
        return nil
    }
}
