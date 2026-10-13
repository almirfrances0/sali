import Combine
import Foundation
import Network

/// What kind of transport the app is currently using to talk to Sali.
public enum TransportMode: String, Codable, Sendable {
    case local    // discovered via Bonjour on the same LAN
    case remote   // Cloudflare Tunnel (or any HTTPS reachable via Internet)
}

/// The observable state of the app's connection to Sali. Named `LinkState` to avoid colliding
/// with `WebSocketClient.ConnectionState` (which is one layer below and answers "is the socket
/// open"; this one answers "which Sali runtime, over which transport, with what reachability").
public enum LinkState: Equatable, Sendable, ConnectionBadgeState {
    case discovering
    case localConnecting(host: String, port: Int)
    case localConnected(host: String, port: Int, latency: TimeInterval?)
    case remoteConnecting
    case remoteConnected(latency: TimeInterval?)
    /// No usable network path — the DEVICE is offline (Wi-Fi + cellular both down). Distinct
    /// from a Sali-unreachable state so the pill can tell the truth about who's absent.
    case deviceOffline
    /// A network path is up but no Sali is reachable on it (LAN empty AND remote probe failed).
    case saliUnreachable
    case reconnecting(previousMode: TransportMode, attempt: Int)

    public var glyphEmoji: String {
        switch self {
        case .localConnected, .remoteConnected: "🟢"
        case .reconnecting, .discovering, .localConnecting, .remoteConnecting: "🟡"
        case .deviceOffline, .saliUnreachable: "⚫"
        }
    }

    public var shortLabel: String {
        switch self {
        case .discovering:            "Finding Sali…"
        case .localConnecting:        "Connecting locally…"
        case .localConnected:         "Sali · Local"
        case .remoteConnecting:       "Connecting…"
        case .remoteConnected:        "Sali · Remote"
        case .reconnecting:           "Reconnecting…"
        case .deviceOffline:          "You're offline"
        case .saliUnreachable:        "Can't reach Sali"
        }
    }

    /// The link, drawn in the app's one state alphabet (`SaliGlyph`) — the same three letters the socket
    /// badge uses, so the header's pill and Settings' row read as one family. A reachable Sali sitting
    /// quietly is the unbroken ring around nothing; "on the way" covers both discovery and reconnection,
    /// since from the outside they are the same wait.
    public var glyph: SaliGlyph {
        switch self {
        case .localConnected, .remoteConnected:                             .idle
        case .discovering, .localConnecting, .remoteConnecting, .reconnecting: .connecting
        case .deviceOffline, .saliUnreachable:                              .offline
        }
    }

    /// The long, spoken form. `shortLabel` stays the terse one set in tracked uppercase; this is what
    /// VoiceOver reads and what a diagnostic line prints, so it names the transport in full words.
    public var label: String {
        switch self {
        case .discovering:                      "Looking for Sali on your network"
        case .localConnecting(let host, _):     "Connecting to Sali at \(host)"
        case .localConnected(let host, _, _):   "Connected to Sali at \(host) on your local network"
        case .remoteConnecting:                 "Connecting to Sali over the Internet"
        case .remoteConnected:                  "Connected to Sali over the Internet"
        case .reconnecting(_, let attempt):     "Reconnecting… (attempt \(attempt))"
        case .deviceOffline:                    "Your device is offline"
        case .saliUnreachable:                  "Sali isn't reachable right now"
        }
    }

    /// True only when a Sali is actually answering — the ink ramp's one question.
    public var isLive: Bool {
        switch self {
        case .localConnected, .remoteConnected: true
        default:                                false
        }
    }

    public var mode: TransportMode? {
        switch self {
        case .localConnecting, .localConnected:            .local
        case .remoteConnecting, .remoteConnected:          .remote
        case .reconnecting(let mode, _):                    mode
        case .discovering, .deviceOffline, .saliUnreachable: nil
        }
    }
}

/// Adapter into AppState. Real production wire uses AppState directly; tests use a stub.
///
/// The three operations the manager needs are:
/// 1. Swap the transport (URL) WITHOUT wiping the event sequence — a LAN↔Remote hop for the
///    SAME Sali (verified by matching /identity runtime_id) preserves chat replay.
/// 2. Swap to a DIFFERENT Sali runtime (rare; only after an unambiguous runtime_id change) —
///    this one MUST reset the sequence because the event table is different.
/// 3. Read the current APIConfiguration + WebSocket state.
@MainActor
public protocol ConnectionApplying: AnyObject {
    /// Same Sali, different transport. Does NOT reset the WS sequence watermark.
    func applyTransport(_ configuration: APIConfiguration, runtimeId: String)

    /// Different Sali (runtime_id changed). Full reset — same as a user-initiated Settings change.
    /// If the manager ever detects a runtime_id change on what it thought was the same peer, it
    /// routes through this path.
    func applyConfigurationFullReset(_ configuration: APIConfiguration)

    /// Access to the TokenStore for the enrolled-runtime-id trust anchor. Kept behind a
    /// small interface so tests can inject their own store.
    var enrolledRuntimeId: String? { get }
    func setEnrolledRuntimeId(_ id: String?)
    var forcedRemote: Bool { get }
    func setForcedRemote(_ v: Bool)

    var currentConfiguration: APIConfiguration { get }
    var isEnrolled: Bool { get }
    var webSocketStatePublisher: AnyPublisher<ConnectionState, Never> { get }
}

/// State machine that decides whether the app should be on LAN or Remote and hands the winning
/// URL to `AppState.applyTransport(_:runtimeId:)`. See `docs/DISCOVERY.md` for the full protocol.
///
/// **Security invariant** (defends against a rogue Bonjour responder on hostile Wi-Fi): once the
/// device has enrolled to a specific Sali (stamped by `TokenStore.enrolledRuntimeId`), the
/// manager REFUSES any LAN candidate whose `/identity` reports a different runtime_id. The
/// runtime_id is captured at enrollment (first successful probe against the URL where the
/// pairing code was redeemed) and is a hard trust anchor from that point on.
@MainActor
public final class ConnectionManager: ObservableObject {
    @Published public private(set) var state: LinkState = .discovering
    @Published public private(set) var lastKnownGoodLocal: DiscoveredSali?
    @Published public private(set) var lastKnownGoodRemote: APIConfiguration?
    @Published public private(set) var lastIdentity: SaliIdentity?
    @Published public private(set) var lastError: String?
    @Published public private(set) var forcedRemote: Bool = false

    // Dependencies
    public let discovery: BonjourDiscovery
    public let pathObserver: NetworkPathObserver
    public let probe: any IdentityProbing
    private weak var applier: ConnectionApplying?
    /// Where "remote" points. A `var`, because it used to be a `let` captured once at init from
    /// whatever configuration existed at launch — on a fresh install, the hardcoded default. Typing
    /// your own URL in onboarding logged you in and then, about a second later, `switchToRemote`
    /// probed this stale value and overwrote the typed URL with the default, persisting it. The
    /// "type your own API URL, it's shareable" feature could never have worked for anyone whose host
    /// was not the default.
    private var remoteFallback: APIConfiguration

    // Machinery
    private var discoveryCancellable: AnyCancellable?
    private var pathCancellable: AnyCancellable?
    private var wsStateCancellable: AnyCancellable?
    private var reconcileTask: Task<Void, Never>?
    private var currentSwapTask: Task<Void, Never>?
    private var lastSwapAt: Date = .distantPast
    private var sawFirstPathSnapshot: Bool = false
    private let minSwapInterval: TimeInterval = 8.0
    // Anti-flap: never restart discovery within this window either — a spurious path flap
    // shouldn't trip the OS permission prompt more than once.
    private let minBrowserRestartInterval: TimeInterval = 3.0
    private var lastBrowserRestartAt: Date = .distantPast

    // Timeouts — raised for LAN because Bonjour resolution + first-hit HTTP over Wi-Fi can be
    // slower than the initial 1.5 s Almir tested against, causing his phone to fall back to
    // remote before local ever responded.
    private let probeTimeoutLocal: TimeInterval = 3.0
    private let probeTimeoutRemote: TimeInterval = 4.0

    public init(
        applier: ConnectionApplying,
        remoteFallback: APIConfiguration = APIConfiguration(environment: .production),
        discovery: BonjourDiscovery? = nil,
        pathObserver: NetworkPathObserver? = nil,
        probe: (any IdentityProbing)? = nil
    ) {
        self.applier = applier
        self.remoteFallback = remoteFallback
        self.discovery = discovery ?? BonjourDiscovery()
        self.pathObserver = pathObserver ?? NetworkPathObserver()
        self.probe = probe ?? IdentityProbe()
    }

    /// Retarget the applier after construction (two-phase init in AppState). Also re-reads the
    /// persisted `forcedRemote` preference from the applier's TokenStore.
    /// Point "remote" at a new host. Called ONLY from the user-initiated Settings/onboarding path
    /// (`AppState.applyConfiguration`) — never from `applyTransport`, which carries LAN URLs and
    /// would make the LAN address the remote fallback the moment you walked out of the house.
    public func setRemoteFallback(_ cfg: APIConfiguration) {
        remoteFallback = cfg
        lastKnownGoodRemote = cfg
    }

    public func rebind(applier: ConnectionApplying) {
        self.applier = applier
        self.forcedRemote = applier.forcedRemote
    }

    /// Kick things off: subscribe to discovery + path + WS state, then decide the first transport.
    /// Idempotent — safe to call on every scenePhase active.
    public func start() {
        guard let applier else { return }
        guard applier.isEnrolled else {
            state = .deviceOffline   // no credentials → no session; user needs to enroll
            return
        }
        if discoveryCancellable == nil {
            discoveryCancellable = discovery.$services
                .receive(on: RunLoop.main)
                .sink { [weak self] _ in self?.scheduleReconcile() }
            pathCancellable = pathObserver.$current
                .receive(on: RunLoop.main)
                .sink { [weak self] _ in
                    self?.sawFirstPathSnapshot = true
                    self?.scheduleReconcile()
                }
            wsStateCancellable = applier.webSocketStatePublisher
                .receive(on: RunLoop.main)
                .sink { [weak self] state in self?.handleWebSocketState(state) }
        }
        pathObserver.start()
        // Gate discovery.start() on a Wi-Fi/wired path so the OS Local Network permission prompt
        // never fires when the user is on cellular. Started on demand from reconcile() when the
        // path is right.
        state = .discovering
        scheduleReconcile()
    }

    /// Tear everything down (e.g. sign out / background).
    public func stop() {
        reconcileTask?.cancel()
        reconcileTask = nil
        currentSwapTask?.cancel()
        currentSwapTask = nil
        discovery.stop()
        pathObserver.stop()
        discoveryCancellable = nil
        pathCancellable = nil
        wsStateCancellable = nil
        sawFirstPathSnapshot = false
        // Deliberately DO NOT set state to .offline here — the WS is still up on background
        // (that's not what stop() is for). The state stays as whatever the user last saw so a
        // scenePhase-quick-flap doesn't repaint. On foreground, start() will re-arm and
        // reconcile.
    }

    /// User pressed "Force Remote" in diagnostics. Persisted so it survives the next reconcile.
    /// Undone via `autoDetect()`.
    public func forceRemote() {
        guard !forcedRemote else { return }
        forcedRemote = true
        applier?.setForcedRemote(true)
        // Kick off a switch to remote immediately if we're not there already.
        scheduleReconcile()
    }

    /// User pressed "Auto-detect" — undo Force Remote. Next reconcile will prefer LAN if available.
    public func autoDetect() {
        guard forcedRemote else { return }
        forcedRemote = false
        applier?.setForcedRemote(false)
        scheduleReconcile()
    }

    /// Diagnostic-sheet action: force a fresh Bonjour discovery cycle. Handy when the browser
    /// silently stopped (permission race, cellular→wifi transition where discovery was gated
    /// off and never re-enabled), or when the user just changed Wi-Fi networks.
    public func refreshDiscovery() {
        lastBrowserRestartAt = .distantPast
        discovery.stop()
        // start() gets called by the next reconcile once path.reach permits it.
        scheduleReconcile()
    }

    /// Diagnostic-sheet action: clear the enrolled-runtime-id trust anchor. Needed when the
    /// anchor was stamped against a previous Sali runtime that no longer exists (e.g. after a
    /// DB restore from an old backup, or Sali migrated to a new PC). The next successful local
    /// probe will re-stamp against the current runtime_id.
    public func resetLocalTrust() {
        applier?.setEnrolledRuntimeId(nil)
        lastError = "local trust anchor cleared — next successful probe re-stamps"
        scheduleReconcile()
    }

    // MARK: - Reconcile loop

    private func scheduleReconcile() {
        // Coalesce a burst of discovery/path events into one decision.
        reconcileTask?.cancel()
        reconcileTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 100_000_000)   // 100ms debounce
            guard !Task.isCancelled, let self else { return }
            await self.reconcile()
        }
    }

    private func reconcile() async {
        // Nothing to reconcile until an applier is bound; the reconcile body reaches it via
        // the helpers below rather than using it directly.
        guard applier != nil else { return }
        let path = pathObserver.current

        // Cold-start guard (UX #9): don't paint .deviceOffline the microsecond before
        // NWPathMonitor delivers its first snapshot. Give it 400ms; if still not delivered,
        // schedule another reconcile and bail this one.
        if !sawFirstPathSnapshot {
            try? await Task.sleep(nanoseconds: 400_000_000)
            if !sawFirstPathSnapshot {
                scheduleReconcile()
                return
            }
        }
        if Task.isCancelled { return }

        if !path.isSatisfied {
            state = .deviceOffline
            return
        }

        // Gate discovery browser on the right path. Never start it on cellular — the OS Local
        // Network permission prompt would fire even though we couldn't use it.
        if path.reach == .wifi || path.reach == .wired {
            let now = Date()
            if !discovery.isBrowsing && now.timeIntervalSince(lastBrowserRestartAt) > minBrowserRestartInterval {
                discovery.start()
                lastBrowserRestartAt = now
            }
        } else {
            discovery.stop()
        }

        // User forced Remote. Skip local.
        if forcedRemote {
            if !isAlreadyConnectedToRemote() {
                await switchToRemote(reason: "forced")
            }
            return
        }

        // Prefer local. On Wi-Fi/wired, if we have a verified candidate and aren't already
        // connected to it, verify and swap. Otherwise fall through to remote.
        if path.reach == .wifi || path.reach == .wired {
            let candidates = Array(discovery.services.values)
            if let candidate = pickBestCandidate(candidates) {
                if !isAlreadyConnectedTo(candidate) {
                    let localDidWork = await tryLocal(candidate)
                    // UX #2: if tryLocal was blocked (flap window, mismatch, probe failed) AND we
                    // are not on any connection right now, fall through to remote so the user
                    // isn't stranded.
                    if !localDidWork && !isAlreadyConnectedTo(anyLocal: true) && !isAlreadyConnectedToRemote() {
                        await switchToRemote(reason: "local unavailable")
                    }
                    return
                } else {
                    return  // already there — nothing to do
                }
            } else if discovery.isBrowsing && state == .discovering {
                // If discovery just started on link-up, give mDNS a brief moment (up to 800ms)
                // to discover the local Sali before declaring none exists.
                try? await Task.sleep(nanoseconds: 800_000_000)
                if Task.isCancelled { return }
                let freshCandidates = Array(discovery.services.values)
                if let freshCandidate = pickBestCandidate(freshCandidates) {
                    if !isAlreadyConnectedTo(freshCandidate) {
                        _ = await tryLocal(freshCandidate)
                    }
                    return
                }
            }
        }
        if !isAlreadyConnectedToRemote() {
            await switchToRemote(reason: "no local candidate")
        }
    }

    private func pickBestCandidate(_ candidates: [DiscoveredSali]) -> DiscoveredSali? {
        // Strong preference: the runtime_id we're ENROLLED to. Weaker preference: the runtime_id
        // of the last successful probe. Otherwise: deterministic (alphabetical Bonjour name).
        let sorted = candidates.sorted { $0.name < $1.name }
        let enrolled = applier?.enrolledRuntimeId
        if let enrolled, let m = sorted.first(where: { $0.runtimeId == enrolled }) {
            return m
        }
        if let sessionPreferred = lastIdentity?.runtimeId,
           let m = sorted.first(where: { $0.runtimeId == sessionPreferred }) {
            return m
        }
        // Fresh-enrollment case (no trust anchor yet): return the first candidate; tryLocal will
        // verify /identity and — critically — the caller must consume `lastIdentity` to STAMP
        // the anchor after enrollment. If a trust anchor IS set, we never return a candidate
        // that doesn't match it (the guard above already returned).
        if enrolled != nil {
            // We had an anchor but no candidate matched. Prefer nothing — the caller should
            // fall through to remote rather than risk a wrong Sali.
            return nil
        }
        return sorted.first
    }

    private func isAlreadyConnectedTo(_ candidate: DiscoveredSali) -> Bool {
        guard case .localConnected(let host, let port, _) = state else { return false }
        return host == candidate.host && port == candidate.port
    }

    private func isAlreadyConnectedTo(anyLocal: Bool) -> Bool {
        switch state {
        case .localConnected: anyLocal
        default: false
        }
    }

    private func isAlreadyConnectedToRemote() -> Bool {
        switch state {
        case .remoteConnected, .remoteConnecting: true
        default: false
        }
    }

    private func withinFlapWindow() -> Bool {
        Date().timeIntervalSince(lastSwapAt) < minSwapInterval
    }

    // MARK: - Local

    /// Attempt to swap to `candidate`. Returns true if a swap happened, false otherwise (so the
    /// caller can decide whether to try remote instead).
    private func tryLocal(_ candidate: DiscoveredSali) async -> Bool {
        if withinFlapWindow() {
            // If we are currently connected to remote (or connecting) and a genuine local
            // candidate was discovered on the network, allow immediate upgrade to local!
            let isUpgradeFromRemote = !isAlreadyConnectedTo(anyLocal: true)
            if !isUpgradeFromRemote {
                // Schedule a retry after the flap window so we re-check cleanly
                let remaining = minSwapInterval - Date().timeIntervalSince(lastSwapAt)
                Task { [weak self] in
                    try? await Task.sleep(nanoseconds: UInt64(max(0.5, remaining) * 1_000_000_000))
                    self?.scheduleReconcile()
                }
                return false
            }
        }
        guard let baseURL = candidate.baseURL else { return false }
        state = .localConnecting(host: candidate.host, port: candidate.port)
        let t0 = Date()
        let identity: SaliIdentity
        do {
            identity = try await probe.probe(baseURL: baseURL, timeout: probeTimeoutLocal)
        } catch {
            lastError = "local probe: \(error.localizedDescription)"
            return false
        }
        if Task.isCancelled { return false }

        // TXT vs /identity mismatch = accidental-lookalike (or a stale TXT record from a rebooted
        // Sali on the same LAN). Still worth checking — soft-warn but proceed, because the
        // bearer-token check at the WebSocket handshake is the actual auth boundary and a
        // rogue peer without valid tokens will simply 4001 us back to remote.
        if let txtRid = candidate.runtimeId, !txtRid.isEmpty, txtRid != identity.runtimeId {
            lastError = "note: TXT runtime_id (\(txtRid.prefix(6))…) differs from /identity (\(identity.runtimeId.prefix(6))…) — proceeding; bearer auth is the gate"
        }
        // TRUST ANCHOR (soft check, Almir §21 "LAN not auto-detecting" fix). If we have an
        // enrolled anchor from a prior successful probe, log the mismatch but PROCEED with the
        // swap. The rationale: (a) bearer-token auth at the WS handshake will reject any peer
        // that isn't the real Sali — a rogue can't produce valid tokens, so a mismatch causes
        // graceful fallback to remote via handleWebSocketState .offline; (b) hard-rejecting on
        // an anchor mismatch permanently breaks LAN discovery after ANY event that changes the
        // runtime_id (Sali's persistent session file wiped, DB restored from an old backup,
        // Almir migrated to a new PC), and Almir has hit exactly that mode. The escape hatch
        // that lets the anchor recover automatically is: on a successful probe, ALWAYS re-stamp
        // the anchor to the current runtime_id (below), so the next reconcile matches.
        if let enrolled = applier?.enrolledRuntimeId, !enrolled.isEmpty, enrolled != identity.runtimeId {
            lastError = "runtime_id changed (\(enrolled.prefix(6))… → \(identity.runtimeId.prefix(6))…) — trusting new"
        }
        // Stamp the anchor to the CURRENT peer's runtime_id (was: only on first-time stamp).
        // This heals stale anchors from wipes/reboots automatically. Almir can also tap
        // "Reset local trust" in the diagnostics sheet if he wants to force-reset.
        if !identity.runtimeId.isEmpty {
            applier?.setEnrolledRuntimeId(identity.runtimeId)
        }

        let latency = Date().timeIntervalSince(t0)
        lastIdentity = identity
        lastKnownGoodLocal = candidate
        lastError = nil
        await applyAndAwaitConnected(
            candidate.configuration!,
            runtimeId: identity.runtimeId,
            targetState: .localConnected(host: candidate.host, port: candidate.port, latency: latency)
        )
        lastSwapAt = Date()
        return true
    }

    // MARK: - Remote

    private func switchToRemote(reason: String) async {
        guard let applier else { return }
        state = .remoteConnecting
        let t0 = Date()
        var latency: TimeInterval?
        var probedIdentity: SaliIdentity?
        do {
            let identity = try await probe.probe(baseURL: remoteFallback.baseURL,
                                                 timeout: probeTimeoutRemote)
            latency = Date().timeIntervalSince(t0)
            probedIdentity = identity
            lastError = nil
        } catch {
            lastError = "remote probe: \(error.localizedDescription) (\(reason))"
        }
        if Task.isCancelled { return }
        lastKnownGoodRemote = remoteFallback

    // SECURITY (F1): same anchor check for remote. If Cloudflare is proxying a DIFFERENT
        // Sali (misconfigured tunnel, migrated backend), refuse quietly rather than sending
        // credentials.
        // A BOOTING Sali is not a different Sali. /identity answers runtime_id "" until the
        // runtime attaches, and "" never matches the enrolled anchor — so every ordinary daemon
        // restart produced the message reserved for a hostile host, and on remote it was a hard
        // refusal with no self-heal, which left an off-LAN phone stranded after an erase. Treat an
        // empty id as "not ready, retry"; only a genuine, non-empty mismatch is a different Sali.
        if let probedIdentity, probedIdentity.runtimeId.isEmpty {
            lastError = "Sali is still starting up — retrying"
            state = .saliUnreachable
            return
        }
        if let probedIdentity, let enrolled = applier.enrolledRuntimeId,
           !enrolled.isEmpty, enrolled != probedIdentity.runtimeId {
            lastError = "remote /identity reports a different Sali (runtime_id doesn't match enrolled)"
            state = .saliUnreachable
            return
        }
        if let probedIdentity {
            lastIdentity = probedIdentity
            // First-time trust after enrollment via remote: stamp the anchor.
            if applier.enrolledRuntimeId == nil, !probedIdentity.runtimeId.isEmpty {
                applier.setEnrolledRuntimeId(probedIdentity.runtimeId)
            }
        }

        // Only swap the config if it's not already on remote. Otherwise the WS re-establishment
        // is entirely the WS layer's job — we just reflect its state.
        let alreadyOnRemote = applier.currentConfiguration.baseURL == remoteFallback.baseURL
        // Adopt the remote configuration ONLY if we actually reached it. This apply used to sit
        // outside the probe result, so a FAILED probe still swapped the whole app onto that host and
        // saved it — replacing a URL that worked with one that had just refused to answer.
        if !alreadyOnRemote, probedIdentity != nil {
            await applyAndAwaitConnected(
                remoteFallback,
                runtimeId: probedIdentity?.runtimeId ?? "",
                targetState: .remoteConnected(latency: latency)
            )
            lastSwapAt = Date()
        } else {
            // UX #5: do NOT set .remoteConnected unless the WS actually is up. Otherwise the
            // pill lies green while the socket is churning. If WS is offline right now, remain
            // in .remoteConnecting; the WS-state observer will move us to .remoteConnected when
            // it lands.
            if case .connected = lastWebSocketState {
                state = .remoteConnected(latency: latency)
            } else if case .connecting = lastWebSocketState {
                state = .remoteConnecting
            } else if lastError != nil, case .idle = lastWebSocketState {
                // The unauth probe failed AND the socket has never come up here — this is a
                // "Sali unreachable" state (probably no Cloudflare tunnel routing to Sali yet,
                // or Sali down). Stay off .remoteConnected.
                state = .saliUnreachable
            }
        }
    }

    /// Hand a new configuration to the app and wait (bounded) for the WebSocket to actually reach
    /// `.connected`. If it doesn't, we stay in `.reconnecting` and let the WS layer retry.
    private func applyAndAwaitConnected(
        _ cfg: APIConfiguration,
        runtimeId: String,
        targetState: LinkState
    ) async {
        guard let applier else { return }
        applier.applyTransport(cfg, runtimeId: runtimeId)
        let deadline = Date().addingTimeInterval(6.0)
        while Date() < deadline {
            if Task.isCancelled { return }
            try? await Task.sleep(nanoseconds: 250_000_000)
            if case .connected = lastWebSocketState { break }
        }
        if case .connected = lastWebSocketState {
            state = targetState
        }
        // If we never reached .connected, the wsState observer already moved us to
        // .reconnecting (or .saliUnreachable via a subsequent handleWebSocketState .offline).
    }

    // MARK: - WebSocket state observer

    private var lastWebSocketState: ConnectionState = .idle

    private func handleWebSocketState(_ ws: ConnectionState) {
        lastWebSocketState = ws
        switch ws {
        case .connected:
            if case .reconnecting(let mode, _) = state {
                if mode == .local, let c = lastKnownGoodLocal {
                    state = .localConnected(host: c.host, port: c.port, latency: nil)
                } else {
                    state = .remoteConnected(latency: nil)
                }
            } else if case .remoteConnecting = state {
                // The "already on remote" branch of switchToRemote deferred to us.
                state = .remoteConnected(latency: nil)
            }
        case .reconnecting(let attempt):
            if let mode = state.mode {
                state = .reconnecting(previousMode: mode, attempt: attempt)
            }
        case .offline:
            // WS layer has given up. Reconcile — try the OTHER transport now.
            if case .localConnected = state {
                state = .reconnecting(previousMode: .local, attempt: 0)
            } else if case .remoteConnected = state {
                state = .reconnecting(previousMode: .remote, attempt: 0)
            }
            scheduleReconcile()
        case .idle, .connecting:
            break
        }
    }
}
