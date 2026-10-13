import Foundation
import Network

/// Coarse snapshot of what the device's active network path is. Enough for the ConnectionManager
/// to decide whether it's worth even TRYING local discovery (no Wi-Fi → skip straight to remote).
public struct NetworkPathSnapshot: Equatable, Sendable {
    public enum Reach: String, Sendable { case wifi, cellular, wired, other, none }
    public let reach: Reach
    public let isExpensive: Bool   // metered path — the OS's "cellular or hotspot" hint
    public let isSatisfied: Bool   // any usable path at all
}

/// Publishes the current network path. Wraps `NWPathMonitor`.
///
/// A cancelled `NWPathMonitor` cannot be restarted — its `pathUpdateHandler` will never fire
/// again. The observer therefore instantiates a fresh monitor on each `start()` and drops the
/// old one on `stop()`, so background→foreground cycles keep working across the whole app
/// lifecycle. Without this, `AppState.stop()` → `AppState.start()` would leave the app blind
/// to reachability forever.
@MainActor
public final class NetworkPathObserver: ObservableObject {
    @Published public private(set) var current: NetworkPathSnapshot =
        NetworkPathSnapshot(reach: .none, isExpensive: false, isSatisfied: false)

    private var monitor: NWPathMonitor?
    private let queue: DispatchQueue

    public init() {
        self.queue = DispatchQueue(label: "sali.net.path", qos: .utility)
    }

    public func start() {
        // Fresh monitor per start — the previous one may have been cancelled by stop() and
        // Apple's contract does not allow a cancelled monitor to restart.
        if monitor != nil { return }
        let m = NWPathMonitor()
        m.pathUpdateHandler = { [weak self] path in
            let snap = Self.snapshot(from: path)
            Task { @MainActor in
                self?.current = snap
            }
        }
        m.start(queue: queue)
        monitor = m
    }

    public func stop() {
        monitor?.cancel()
        monitor = nil
        // Reset the published snapshot so a subsequent start() doesn't inherit stale reachability
        // from a previous session. .isSatisfied = false forces the manager to WAIT for a fresh
        // callback before deciding anything.
        current = NetworkPathSnapshot(reach: .none, isExpensive: false, isSatisfied: false)
    }

    // Runs on the monitor's own queue, not the main actor — the class-level @MainActor would
    // otherwise be inherited and make this uncallable from `pathUpdateHandler`.
    private nonisolated static func snapshot(from path: NWPath) -> NetworkPathSnapshot {
        let reach: NetworkPathSnapshot.Reach
        if path.status != .satisfied {
            reach = .none
        } else if path.usesInterfaceType(.wifi) {
            reach = .wifi
        } else if path.usesInterfaceType(.cellular) {
            reach = .cellular
        } else if path.usesInterfaceType(.wiredEthernet) {
            reach = .wired
        } else {
            reach = .other
        }
        return NetworkPathSnapshot(
            reach: reach,
            isExpensive: path.isExpensive,
            isSatisfied: path.status == .satisfied
        )
    }
}
