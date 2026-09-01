import Foundation

/// Persists the device's credentials. Tokens go to the Keychain; non-secret metadata (device id, role,
/// expiries, the resolved configuration) goes to UserDefaults so the app can render its authorization state
/// and decide when to refresh without touching the Keychain on every read.
public final class TokenStore: @unchecked Sendable {
    private enum K {
        static let access = "sali.accessToken"
        static let refresh = "sali.refreshToken"
        static let deviceId = "sali.deviceId"
        static let role = "sali.role"
        static let accessExpiry = "sali.accessExpiry"
        static let config = "sali.config"
    }

    private let defaults: UserDefaults
    private let lock = NSLock()

    public init(defaults: UserDefaults = .standard) { self.defaults = defaults }

    // MARK: credentials

    public var accessToken: String? { Keychain.get(K.access) }
    public var refreshToken: String? { Keychain.get(K.refresh) }
    public var deviceId: String? { defaults.string(forKey: K.deviceId) }
    public var role: Role { Role(rawValue: defaults.string(forKey: K.role) ?? "") ?? .observer }
    public var isEnrolled: Bool { accessToken != nil && refreshToken != nil }

    /// Whether the access token is at/near expiry (30s skew) — cheap check to refresh proactively.
    public var accessExpiredOrExpiring: Bool {
        let ts = defaults.double(forKey: K.accessExpiry)
        guard ts > 0 else { return true }
        return Date().timeIntervalSince1970 >= (ts - 30)
    }

    public func save(session: IssuedSession) {
        lock.lock(); defer { lock.unlock() }
        try? Keychain.set(session.accessToken, for: K.access)
        try? Keychain.set(session.refreshToken, for: K.refresh)
        defaults.set(session.deviceId, forKey: K.deviceId)
        defaults.set(session.role, forKey: K.role)
        defaults.set(session.accessExpiresAt?.timeIntervalSince1970 ?? 0, forKey: K.accessExpiry)
    }

    public func clear() {
        lock.lock(); defer { lock.unlock() }
        Keychain.delete(K.access)
        Keychain.delete(K.refresh)
        defaults.removeObject(forKey: K.deviceId)
        defaults.removeObject(forKey: K.role)
        defaults.removeObject(forKey: K.accessExpiry)
    }

    // MARK: configuration (environment / base URL)

    public func loadConfiguration() -> APIConfiguration {
        guard let data = defaults.data(forKey: K.config),
              let cfg = try? JSONDecoder().decode(APIConfiguration.self, from: data) else {
            return APIConfiguration(environment: .production)
        }
        return cfg
    }

    public func saveConfiguration(_ cfg: APIConfiguration) {
        if let data = try? JSONEncoder().encode(cfg) { defaults.set(data, forKey: K.config) }
    }
}
