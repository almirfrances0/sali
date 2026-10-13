import Foundation
import Network
import os

/// A Sali service the local browser found on the same Wi-Fi/LAN. Not yet verified — the caller
/// must hit `IdentityProbe` before trusting it.
public struct DiscoveredSali: Equatable, Sendable, Codable {
    public let host: String        // resolved host or IPv4 (never an mDNS-only name)
    public let port: Int
    public let runtimeId: String?  // from the TXT record; nil if the peer didn't advertise it
    public let name: String        // full Bonjour instance name — for diagnostics only
    public let version: String?    // from the TXT record; informational

    /// The URL a client should hit for the unauth /identity verifier before opening a
    /// bearer-token session. Always plain `http://` on the LAN — TLS is not viable link-local
    /// without a certificate chain a phone would trust; the security model is bearer-token,
    /// not transport-based (§ auth is IP-agnostic).
    public var baseURL: URL? {
        URL(string: "http://\(host):\(port)")
    }

    /// The `APIConfiguration` this candidate maps to. Environment stays `.development` (the enum
    /// distinguishes hardcoded endpoints, not transport modes); the base URL is the discovered
    /// one, and everything derived from it (apiRoot, webSocketURL) flows through the existing
    /// `APIConfiguration` machinery unchanged.
    public var configuration: APIConfiguration? {
        guard let url = baseURL else { return nil }
        return APIConfiguration(environment: .development, baseURL: url)
    }
}

/// mDNS/Bonjour browser for the `_sali._tcp` service Sali's backend advertises.
///
/// This layer is INFORMATION-ONLY. It says "a service appeared here" or "it went away." It does
/// not open any authenticated connection; that's the ConnectionManager's job after it verifies
/// the peer via the unauth `/identity` endpoint (see `IdentityProbe`).
///
/// Uses `Network.framework`'s `NWBrowser`, which requires the app to declare `NSBonjourServices`
/// and (on iOS 14+) will prompt the user for Local Network permission the first time the browser
/// starts. On denial, the browser silently returns no results and the app falls back to remote.
@MainActor
public final class BonjourDiscovery: ObservableObject {
    /// Currently visible Sali services on this link, keyed by full Bonjour name so a re-appearance
    /// after a DHCP renewal collapses into an update rather than a duplicate row.
    @Published public private(set) var services: [String: DiscoveredSali] = [:]

    /// True while the browser is actively listening. Screens observe this to decide whether to
    /// show a spinner or "no LAN Sali found" copy.
    @Published public private(set) var isBrowsing: Bool = false

    /// True when Local Network permission has been explicitly denied by the user. Determined
    /// heuristically: browser state goes to `.failed(.dns(-72007))` on denial (the "no
    /// permission" DNS error). Not an error — the app must still work over Cloudflare when this
    /// is true.
    @Published public private(set) var permissionDenied: Bool = false

    private var browser: NWBrowser?
    private let serviceType: String
    /// The set of Bonjour service names present in the LAST handleResults snapshot. A slow
    /// resolveEndpoint completion writes services[name] ONLY when name is still in this set —
    /// otherwise the write is a stale-late arrival for a service Bonjour has already withdrawn,
    /// and re-inserting it would keep a phantom candidate that the ConnectionManager then
    /// wastes a 3s probe timeout on before falling back to remote.
    private var currentlyPresent: Set<String> = []

    public init(serviceType: String = "_sali._tcp") {
        self.serviceType = serviceType
    }

    /// Start browsing. Idempotent — a second call while already browsing is a no-op.
    public func start() {
        if browser != nil { return }
        let params = NWParameters()
        params.includePeerToPeer = false   // no AWDL; only real LAN/Wi-Fi
        let descriptor = NWBrowser.Descriptor.bonjourWithTXTRecord(type: serviceType, domain: nil)
        let b = NWBrowser(for: descriptor, using: params)
        browser = b

        b.stateUpdateHandler = { [weak self] state in
            Task { @MainActor in
                guard let self else { return }
                switch state {
                case .ready:
                    self.isBrowsing = true
                    self.permissionDenied = false
                case .failed(let error):
                    self.isBrowsing = false
                    // NWError.dns(-72007) is the "user denied local network" signal on iOS 14+.
                    // The error type is not always -72007 exactly across iOS versions; treat any
                    // "not authorized" pattern as a denial, then let the ConnectionManager fall
                    // back cleanly to remote. Never crash on a permission decision.
                    if error.errorCode == -65555 || error.errorCode == -72007 ||
                       "\(error)".localizedCaseInsensitiveContains("not authorized") ||
                       "\(error)".localizedCaseInsensitiveContains("permission") {
                        self.permissionDenied = true
                    }
                case .cancelled:
                    self.isBrowsing = false
                default:
                    break
                }
            }
        }

        b.browseResultsChangedHandler = { [weak self] results, _ in
            Task { @MainActor in
                self?.handleResults(results)
            }
        }

        b.start(queue: .main)
    }

    /// Stop browsing. Idempotent. Discovered services are cleared so screens don't linger on
    /// stale rows the user can no longer connect to. `permissionDenied` is ALSO cleared — if the
    /// user grants permission from Settings and comes back, the next `start()` will re-probe
    /// and update the flag when the browser transitions to .ready or .failed again.
    public func stop() {
        browser?.cancel()
        browser = nil
        services = [:]
        isBrowsing = false
        permissionDenied = false
    }

    private func handleResults(_ results: Set<NWBrowser.Result>) {
        // Snapshot presence FIRST so both resolver-late-arrivals and the prune loop below agree
        // on what Bonjour just reported. A slow resolver that completes AFTER the service left
        // the LAN must not re-insert a stale entry — it does the presence check against this set.
        let present: Set<String> = Set(results.compactMap { r in
            if case let .service(name, _, _, _) = r.endpoint { return name } else { return nil }
        })
        currentlyPresent = present

        for result in results {
            guard case let .service(name, type, _, _) = result.endpoint, type == serviceType else {
                continue
            }
            var runtimeId: String?
            var version: String?
            if case let .bonjour(txt) = result.metadata {
                runtimeId = txt["runtime_id"]
                version = txt["version"]
            }
            // Kick off a background resolve; on completion, gate insertion on "the service is
            // STILL in currentlyPresent." Otherwise a resolver that finishes ~1-2s later for a
            // service Bonjour has since withdrawn re-adds it, ConnectionManager then wastes a
            // 3s tryLocal probe on a phantom before falling back to remote — exactly the
            // latency the LAN discovery flow exists to spare.
            resolveEndpoint(result.endpoint) { [weak self] resolved in
                Task { @MainActor in
                    guard let self, let resolved = resolved else { return }
                    guard self.currentlyPresent.contains(name) else { return }
                    self.services[name] = DiscoveredSali(
                        host: resolved.host,
                        port: resolved.port,
                        runtimeId: runtimeId,
                        name: name,
                        version: version
                    )
                }
            }
        }
        // Prune anything that disappeared from Bonjour (device left, Sali stopped). Same present
        // set the resolver closures gate against — the two paths stay consistent.
        for existing in Array(services.keys) where !present.contains(existing) {
            services.removeValue(forKey: existing)
        }
    }

    /// Resolve a Bonjour endpoint to a concrete (IPv4-preferred) host:port. Uses a throwaway
    /// NWConnection in "get remote endpoint" mode; ideal is to prefer IPv4 because URLSession's
    /// HTTP-to-local sometimes rejects IPv6 link-local addresses. Falls back to the resolved
    /// hostname if IP is unavailable.
    private nonisolated func resolveEndpoint(
        _ endpoint: NWEndpoint,
        completion: @escaping @Sendable (Resolved?) -> Void
    ) {
        let params = NWParameters.tcp
        params.prohibitedInterfaceTypes = [.cellular]
        // Preferring IPv4 avoids some URLSession quirks with link-local IPv6 when hitting a plain
        // http:// URL. Both work at the TCP layer; IPv4 is simpler for a URL string.
        if let ip = params.defaultProtocolStack.internetProtocol as? NWProtocolIP.Options {
            ip.version = .v4
        }
        let conn = NWConnection(to: endpoint, using: params)
        let didComplete = OSAllocatedUnfairLock(initialState: false)

        // Safety timeout in case NWConnection remains in .preparing
        Task {
            try? await Task.sleep(nanoseconds: 3_000_000_000) // 3 seconds
            let shouldCancel = didComplete.withLock { completed -> Bool in
                if !completed {
                    completed = true
                    return true
                }
                return false
            }
            if shouldCancel {
                conn.cancel()
                completion(nil)
            }
        }

        conn.stateUpdateHandler = { state in
            switch state {
            case .ready:
                let shouldHandle = didComplete.withLock { completed -> Bool in
                    if !completed {
                        completed = true
                        return true
                    }
                    return false
                }
                guard shouldHandle else { return }

                if let remote = conn.currentPath?.remoteEndpoint,
                   case let .hostPort(host, port) = remote {
                    let hostStr: String
                    switch host {
                    case .ipv4(let addr):
                        let raw = addr.rawValue
                        if raw.count == 4 {
                            hostStr = "\(raw[0]).\(raw[1]).\(raw[2]).\(raw[3])"
                        } else {
                            hostStr = addr.debugDescription.components(separatedBy: "%").first ?? addr.debugDescription
                        }
                    case .ipv6(let addr):
                        let rawStr = addr.debugDescription.components(separatedBy: "%").first ?? addr.debugDescription
                        hostStr = "[\(rawStr)]"
                    case .name(let name, _):
                        hostStr = name.components(separatedBy: "%").first ?? name
                    @unknown default:
                        hostStr = ""
                    }
                    conn.cancel()
                    if !hostStr.isEmpty {
                        completion(Resolved(host: hostStr, port: Int(port.rawValue)))
                    } else {
                        completion(nil)
                    }
                } else {
                    conn.cancel()
                    completion(nil)
                }
            case .failed, .cancelled:
                let shouldHandle = didComplete.withLock { completed -> Bool in
                    if !completed {
                        completed = true
                        return true
                    }
                    return false
                }
                if shouldHandle {
                    completion(nil)
                }
            default:
                break
            }
        }
        conn.start(queue: .global(qos: .userInitiated))
    }

    fileprivate struct Resolved: Sendable {
        let host: String
        let port: Int
    }
}
