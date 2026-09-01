import Foundation

/// Owns the device's authorization lifecycle (§23-§24): enroll with a one-time code, persist tokens in the
/// Keychain, expose the current role, and sign out (local wipe). Enrollment and refresh are unauthenticated
/// endpoints that carry their own secret in the body, so they use a bare URLSession — not the APIClient's
/// authorized path.
@MainActor
public final class AuthService: ObservableObject {
    @Published public private(set) var isEnrolled: Bool
    @Published public private(set) var role: Role
    @Published public private(set) var deviceName: String

    private let tokenStore: TokenStore
    private let session: URLSession
    private var configuration: APIConfiguration

    public init(tokenStore: TokenStore, configuration: APIConfiguration, session: URLSession = .shared) {
        self.tokenStore = tokenStore
        self.session = session
        self.configuration = configuration
        self.isEnrolled = tokenStore.isEnrolled
        self.role = tokenStore.role
        self.deviceName = UserDefaults.standard.string(forKey: "sali.deviceName") ?? ""
    }

    public func updateConfiguration(_ cfg: APIConfiguration) { self.configuration = cfg }

    /// Redeem a one-time pairing code minted on the host (`sali enroll-code`). On success the tokens are
    /// stored in the Keychain and the device becomes authorized.
    public func enroll(code: String, deviceName: String, model: String? = nil) async throws {
        let body: [String: Any] = [
            "code": code.trimmingCharacters(in: .whitespacesAndNewlines),
            "name": deviceName, "model": model as Any, "platform": "ios",
        ].compactMapValues { ($0 is NSNull) ? nil : $0 }

        var req = URLRequest(url: configuration.apiRoot.appendingPathComponent("enroll"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: body)

        let (data, resp): (Data, URLResponse)
        do { (data, resp) = try await session.data(for: req) }
        catch { throw APIError.offline }

        guard let http = resp as? HTTPURLResponse else { throw APIError.offline }
        if http.statusCode == 401 { throw APIError.forbidden("Invalid or expired pairing code.") }
        guard http.statusCode == 200,
              let issued = try? APIClient.decoder.decode(IssuedSession.self, from: data) else {
            throw APIError.server(http.statusCode, "enrollment failed")
        }
        tokenStore.save(session: issued)
        UserDefaults.standard.set(deviceName, forKey: "sali.deviceName")
        self.deviceName = deviceName
        self.role = Role(rawValue: issued.role) ?? .observer
        self.isEnrolled = true
    }

    /// Local sign-out: wipe credentials from this device. (To revoke server-side, an owner uses Settings →
    /// Devices → Revoke, which kills the sessions even if the device is offline.)
    public func signOut() {
        tokenStore.clear()
        isEnrolled = false
        role = .observer
    }

    /// Called by APIClient when refresh fails — the device is no longer trusted.
    public func handleAuthLost() {
        tokenStore.clear()
        isEnrolled = false
    }
}
