import Foundation

/// The public metadata Sali's `/identity` endpoint returns. Read BEFORE the app has any
/// credentials — its purpose is to prove "this responder is a Sali" before we spend a bearer
/// token on it. Never carries tokens (see backend `test_identity_never_leaks_api_token_or_bearer`).
public struct SaliIdentity: Equatable, Sendable, Codable {
    public let service: String       // must equal "sali"
    public let runtimeId: String     // stable UUID across daemon restarts; matches Bonjour TXT
    public let version: String?
    public let hostname: String?
    public let protocolVersion: String?

    public init(
        service: String,
        runtimeId: String,
        version: String? = nil,
        hostname: String? = nil,
        protocolVersion: String? = nil
    ) {
        self.service = service
        self.runtimeId = runtimeId
        self.version = version
        self.hostname = hostname
        self.protocolVersion = protocolVersion
    }

    private enum CodingKeys: String, CodingKey {
        case service, version, hostname
        case runtimeId = "runtime_id"
        case protocolVersion = "protocol"
    }
}

public enum IdentityProbeError: Error, LocalizedError, Sendable {
    case notReachable                      // TCP/HTTP failure
    case timeout                           // exceeded the caller's budget
    case httpStatus(Int)                   // non-200 (e.g. 404 — wrong path, wrong app)
    case notSali(String)                   // /identity replied but service != "sali"
    case malformed(String)                 // JSON present but missing required fields

    public var errorDescription: String? {
        switch self {
        case .notReachable:       "Not reachable"
        case .timeout:            "Timed out"
        case .httpStatus(let c):  "HTTP \(c)"
        case .notSali(let s):     "Not Sali (service=\(s))"
        case .malformed(let m):   "Malformed identity response: \(m)"
        }
    }
}

/// The abstraction the ConnectionManager depends on. A protocol so tests can supply a stub
/// without needing to mock URLSession or subclass an actor.
public protocol IdentityProbing: Sendable {
    func probe(baseURL: URL, timeout: TimeInterval) async throws -> SaliIdentity
}

/// Probes a candidate URL's `/identity` endpoint to verify it's a Sali BEFORE opening an
/// authenticated session there. Discovery alone (mDNS) is not authorization; this is the
/// verification step in between.
///
/// Design constraint (security): the probe MUST NOT send any bearer token. `/identity` is unauth
/// by contract; if a probe sends credentials to an unverified host, a lookalike on the LAN could
/// harvest them. The URLSession config here forbids credential propagation.
public actor IdentityProbe: IdentityProbing {
    private let session: URLSession

    public init(session: URLSession? = nil) {
        if let session {
            self.session = session
        } else {
            let cfg = URLSessionConfiguration.ephemeral   // no cookies, no cache, no credential store
            cfg.timeoutIntervalForRequest = 2.0
            cfg.timeoutIntervalForResource = 4.0
            cfg.httpAdditionalHeaders = [:]               // no shared auth headers ride along
            self.session = URLSession(configuration: cfg)
        }
    }

    /// Probe `baseURL/identity`. Timeout is bounded per call; caller should keep this fast
    /// (~1–2s) so a dead LAN candidate doesn't block the remote fallback.
    public func probe(baseURL: URL, timeout: TimeInterval = 2.0) async throws -> SaliIdentity {
        let url = baseURL.appendingPathComponent("identity")
        var request = URLRequest(url: url, timeoutInterval: timeout)
        request.httpMethod = "GET"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        // Belt-and-suspenders: strip any Authorization header even if the caller-shared session
        // had one. The whole point of the probe is to hit /identity unauthenticated.
        request.setValue(nil, forHTTPHeaderField: "Authorization")

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch let err as URLError {
            if err.code == .timedOut { throw IdentityProbeError.timeout }
            throw IdentityProbeError.notReachable
        } catch {
            throw IdentityProbeError.notReachable
        }
        guard let http = response as? HTTPURLResponse else {
            throw IdentityProbeError.notReachable
        }
        guard http.statusCode == 200 else {
            throw IdentityProbeError.httpStatus(http.statusCode)
        }

        let identity: SaliIdentity
        do {
            identity = try JSONDecoder().decode(SaliIdentity.self, from: data)
        } catch {
            throw IdentityProbeError.malformed(String(describing: error))
        }
        guard identity.service == "sali" else {
            throw IdentityProbeError.notSali(identity.service)
        }
        return identity
    }
}
