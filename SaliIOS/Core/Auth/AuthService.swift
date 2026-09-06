import Foundation

/// Owns the app's authorization lifecycle (password auth): log in with the owner PASSWORD, persist the
/// password + tokens in the Keychain, expose the current role, and sign out (local wipe). Login and refresh
/// are unauthenticated endpoints that carry their own secret in the body, so they use a bare URLSession —
/// not the APIClient's authorized path.
@MainActor
public final class AuthService: ObservableObject {
    /// Logged in ⇔ a password is stored (NOT a token) — an expired token can never drop this.
    @Published public private(set) var isLoggedIn: Bool
    @Published public private(set) var role: Role
    @Published public private(set) var deviceName: String

    private let tokenStore: TokenStore
    private let session: URLSession
    private var configuration: APIConfiguration

    public init(tokenStore: TokenStore, configuration: APIConfiguration, session: URLSession = .shared) {
        self.tokenStore = tokenStore
        self.session = session
        self.configuration = configuration
        self.isLoggedIn = tokenStore.isLoggedIn
        self.role = tokenStore.role
        self.deviceName = UserDefaults.standard.string(forKey: "sali.deviceName") ?? ""
    }

    public func updateConfiguration(_ cfg: APIConfiguration) { self.configuration = cfg }

    /// Authenticate with the owner password. On success the PASSWORD (the durable credential) and the tokens
    /// are stored, and the app is logged in. Because the app re-authenticates with the stored password
    /// whenever a token expires, this screen is never shown again for an expired session — only a genuinely
    /// wrong password (the host ran `sali change-password`) brings it back.
    public func login(password: String, deviceName: String, model: String? = nil) async throws {
        let trimmed = deviceName.trimmingCharacters(in: .whitespacesAndNewlines)
        let name = trimmed.isEmpty ? "iPhone" : trimmed
        let body: [String: Any] = [
            "password": password, "name": name, "model": model as Any, "platform": "ios",
        ].compactMapValues { ($0 is NSNull) ? nil : $0 }

        var req = URLRequest(url: configuration.apiRoot.appendingPathComponent("auth/login"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: body)

        let (data, resp): (Data, URLResponse)
        do { (data, resp) = try await session.data(for: req) }
        catch { throw APIError.offline }

        guard let http = resp as? HTTPURLResponse else { throw APIError.offline }
        if http.statusCode == 401 { throw APIError.forbidden("Incorrect password.") }
        if http.statusCode == 429 { throw APIError.server(429, "Too many attempts — wait a few minutes.") }
        guard http.statusCode == 200,
              let issued = try? APIClient.decoder.decode(IssuedSession.self, from: data) else {
            throw APIError.server(http.statusCode, "login failed")
        }
        tokenStore.savePassword(password)
        tokenStore.save(session: issued)
        guard tokenStore.isLoggedIn else {
            throw APIError.server(500, "Failed to save the password on this device.")
        }
        UserDefaults.standard.set(name, forKey: "sali.deviceName")
        self.deviceName = name
        self.role = Role(rawValue: issued.role) ?? .observer
        self.isLoggedIn = true
    }

    /// Local sign-out: wipe the password + tokens from this device. The next launch shows the login screen.
    /// (To cut a lost phone off from the server, run `sali change-password` on the host — it revokes every
    /// session immediately, even while the phone is offline.)
    public func signOut() {
        tokenStore.clear()
        isLoggedIn = false
        role = .observer
    }

    /// Called by APIClient when a re-login with the stored password is DEFINITIVELY rejected (a 401 — the
    /// password was rotated on the host). Only this — never a transient network failure — logs the app out.
    public func handleAuthLost() {
        tokenStore.clear()
        isLoggedIn = false
    }
}
