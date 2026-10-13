import Foundation

/// Persists the device's credentials. Tokens go to the Keychain; non-secret metadata (device id, role,
/// expiries, the resolved configuration) goes to UserDefaults so the app can render its authorization state
/// and decide when to refresh without touching the Keychain on every read.
public final class TokenStore: @unchecked Sendable {
    private enum K {
        static let access = "sali.accessToken"
        static let refresh = "sali.refreshToken"
        /// The login PASSWORD — the durable credential (password auth). Kept in the Keychain, and it is
        /// what makes lockout impossible: the app re-authenticates with it whenever a token expires, so an
        /// expired/revoked token never forces the user back to onboarding. Cleared only on an explicit sign
        /// out or a definitive wrong-password 401 (i.e. the password was rotated on the host).
        static let password = "sali.password"
        static let deviceId = "sali.deviceId"
        static let role = "sali.role"
        static let accessExpiry = "sali.accessExpiry"
        static let config = "sali.config"
        /// The runtime_id of the Sali we're enrolled to. Set once on first successful `/identity`
        /// probe after enrollment; used ever after as a TRUST ANCHOR by `ConnectionManager` — any
        /// LAN discovery candidate whose `/identity` reports a different runtime_id is REFUSED,
        /// even before the bearer token is offered. This is the defense against a rogue Bonjour
        /// responder on hostile Wi-Fi harvesting the token. Cleared on signOut() only.
        static let enrolledRuntimeId = "sali.enrolledRuntimeId"
        /// User preference: force remote transport (skip LAN discovery). Persisted so a tap on
        /// "Force Remote" in the diagnostics sheet doesn't silently revert on the next Bonjour
        /// ping. Cleared explicitly via "Auto-detect".
        static let forcedRemote = "sali.forcedRemote"
    }

    private let defaults: UserDefaults
    private let lock = NSLock()

    public init(defaults: UserDefaults = .standard) { self.defaults = defaults }

    // MARK: credentials

    public var accessToken: String? { Keychain.get(K.access) ?? defaults.string(forKey: K.access) }
    public var refreshToken: String? { Keychain.get(K.refresh) ?? defaults.string(forKey: K.refresh) }
    /// The stored login password — the durable credential the app re-authenticates with.
    public var password: String? { Keychain.get(K.password) ?? defaults.string(forKey: K.password) }
    public var deviceId: String? { defaults.string(forKey: K.deviceId) }
    public var role: Role { Role(rawValue: defaults.string(forKey: K.role) ?? "") ?? .observer }

    /// Logged in ⇔ a password is stored — NOT a token's presence or expiry. This is the whole point of
    /// password auth: an expired or missing token can never drop the login gate, because the app can always
    /// re-mint a token from the password. Only an explicit sign-out or a definitive wrong-password 401
    /// (the host rotated the password) clears it.
    public var isLoggedIn: Bool {
        guard let pw = password, !pw.isEmpty else { return false }
        return true
    }

    /// Whether the user has ever entered/saved a server URL. Onboarding requires it, so first launch (no
    /// saved URL) shows the URL+password screen instead of assuming any hardcoded host.
    public var hasConfiguration: Bool { defaults.data(forKey: K.config) != nil }

    /// Whether the access token is at/near expiry (30s skew) — cheap check to refresh proactively.
    public var accessExpiredOrExpiring: Bool {
        let ts = defaults.double(forKey: K.accessExpiry)
        guard ts > 0 else { return true }
        return Date().timeIntervalSince1970 >= (ts - 30)
    }

    public func save(session: IssuedSession) {
        lock.lock(); defer { lock.unlock() }
        do {
            try Keychain.set(session.accessToken, for: K.access)
            defaults.removeObject(forKey: K.access)
        } catch {
            defaults.set(session.accessToken, forKey: K.access)
        }

        do {
            try Keychain.set(session.refreshToken, for: K.refresh)
            defaults.removeObject(forKey: K.refresh)
        } catch {
            defaults.set(session.refreshToken, forKey: K.refresh)
        }

        defaults.set(session.deviceId, forKey: K.deviceId)
        defaults.set(session.role, forKey: K.role)
        defaults.set(session.accessExpiresAt?.timeIntervalSince1970 ?? 0, forKey: K.accessExpiry)
    }

    /// Persist the login password (the durable credential). Keychain, with a UserDefaults fallback exactly
    /// like the tokens, so a Keychain write failure never leaves the app unable to re-authenticate.
    public func savePassword(_ password: String) {
        lock.lock(); defer { lock.unlock() }
        do {
            try Keychain.set(password, for: K.password)
            defaults.removeObject(forKey: K.password)
        } catch {
            defaults.set(password, forKey: K.password)
        }
    }

    public func clear() {
        lock.lock(); defer { lock.unlock() }
        Keychain.delete(K.access)
        Keychain.delete(K.refresh)
        Keychain.delete(K.password)
        defaults.removeObject(forKey: K.access)
        defaults.removeObject(forKey: K.refresh)
        defaults.removeObject(forKey: K.password)
        defaults.removeObject(forKey: K.deviceId)
        defaults.removeObject(forKey: K.role)
        defaults.removeObject(forKey: K.accessExpiry)
        // The trust anchor is bound to the enrolled Sali; a signout drops it. The forcedRemote
        // preference is a per-user choice, not a security boundary — leave it alone across
        // sign-outs so the user's stated preference persists.
        defaults.removeObject(forKey: K.enrolledRuntimeId)
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

    // MARK: LAN discovery — trust anchor + user preference

    /// The `runtime_id` this device is enrolled to. Once set (on the FIRST successful `/identity`
    /// probe after enrollment) it acts as a trust anchor: `ConnectionManager` refuses any LAN
    /// candidate whose `/identity` reports a different runtime_id, defeating a rogue Bonjour
    /// responder that might otherwise trick the app into shipping its bearer token to an
    /// attacker on the same Wi-Fi.
    public var enrolledRuntimeId: String? {
        get { defaults.string(forKey: K.enrolledRuntimeId) }
        set {
            if let v = newValue, !v.isEmpty {
                defaults.set(v, forKey: K.enrolledRuntimeId)
            } else {
                defaults.removeObject(forKey: K.enrolledRuntimeId)
            }
        }
    }

    /// User preference for Force Remote. Persisted so the setting survives the next Bonjour ping.
    public var forcedRemote: Bool {
        get { defaults.bool(forKey: K.forcedRemote) }
        set { defaults.set(newValue, forKey: K.forcedRemote) }
    }
}
