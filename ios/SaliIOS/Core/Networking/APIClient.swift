import Foundation

/// The single REST client. Every request carries the device access token; on a 401 it refreshes ONCE
/// (single-flight, concurrent callers await the same refresh) and retries. If refresh fails, it signals
/// `.unauthorized`/`.notEnrolled` so the app returns to onboarding. Feature code never builds URLs or
/// touches tokens directly — it calls typed helpers here (§3 "don't couple screens to HTTP").
public actor APIClient {
    private let session: URLSession
    private let tokenStore: TokenStore
    private var configuration: APIConfiguration
    private var refreshTask: Task<Bool, Never>?

    /// Called when the device is no longer trusted (refresh failed) so the app can drop to onboarding.
    public var onAuthLost: (@Sendable () -> Void)?

    public init(tokenStore: TokenStore, configuration: APIConfiguration,
                session: URLSession = .shared,
                onAuthLost: (@Sendable () -> Void)? = nil) {
        self.tokenStore = tokenStore
        self.configuration = configuration
        self.session = session
        // Installed via init so there is no race with the first REST call. The previous form
        // (a Task { await client.setOnAuthLost(...) } inside AppState.init) could schedule after
        // start()'s first refreshSnapshot, and a genuine 401 in that microscopic window would
        // be silently dropped instead of routed to onboarding.
        self.onAuthLost = onAuthLost
    }

    public func updateConfiguration(_ cfg: APIConfiguration) { self.configuration = cfg }
    public func currentConfiguration() -> APIConfiguration { configuration }
    public func setOnAuthLost(_ handler: @escaping @Sendable () -> Void) { self.onAuthLost = handler }

    public func refreshTokens() async -> Bool {
        await refreshIfPossible()
    }

    static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        // LENIENT ON PURPOSE. A throwing date strategy fails the WHOLE response, so one oddly-formatted
        // timestamp anywhere blanks an entire screen — that has happened three times in this app: a
        // timezone-naive `next_wakeup` silently emptied Life, space-separated stamps emptied the task
        // history, and archive rows failed wholesale. The host builds timestamps several ways (asyncpg
        // ISO, `str(datetime)` with a space, naive values with no offset), and a client cannot make the
        // server consistent. So: accept every shape the host actually emits, and if a value is genuinely
        // unreadable return .distantPast rather than throwing — a wrong-looking date costs that one
        // field, never the screen. The sentinel does NOT reach the user as a date: `Date.saliRelative`
        // and the day-grouping helpers test `saliIsUnknownTime` and say "at an unknown time"/"Undated",
        // because "56 yr. ago" stated confidently is worse than admitting the stamp was unreadable.
        d.dateDecodingStrategy = .custom { decoder in
            let raw = try decoder.singleValueContainer().decode(String.self)
            let s = raw.trimmingCharacters(in: .whitespaces)
            let iso = ISO8601DateFormatter()
            for opts in [[.withInternetDateTime, .withFractionalSeconds],
                         [.withInternetDateTime]] as [ISO8601DateFormatter.Options] {
                iso.formatOptions = opts
                if let date = iso.date(from: s) { return date }
                // Postgres/`str(datetime)` uses a space where ISO-8601 wants a T.
                if let date = iso.date(from: s.replacingOccurrences(of: " ", with: "T")) { return date }
            }
            // Timezone-naive ("2026-09-02T22:00:34.833226") — assume UTC rather than discarding it.
            let naive = DateFormatter()
            naive.locale = Locale(identifier: "en_US_POSIX")
            naive.timeZone = TimeZone(identifier: "UTC")
            for fmt in ["yyyy-MM-dd'T'HH:mm:ss.SSSSSS", "yyyy-MM-dd'T'HH:mm:ss.SSS",
                        "yyyy-MM-dd'T'HH:mm:ss", "yyyy-MM-dd HH:mm:ss", "yyyy-MM-dd"] {
                naive.dateFormat = fmt
                if let date = naive.date(from: s.replacingOccurrences(of: " ", with: "T")) { return date }
                if let date = naive.date(from: s) { return date }
            }
            return .distantPast
        }
        return d
    }()

    // MARK: - Typed verbs

    public func get<T: Decodable>(_ path: String, query: [String: String] = [:]) async throws -> T {
        try await request(path, method: "GET", query: query, body: nil)
    }

    @discardableResult
    public func post<T: Decodable>(_ path: String, json: [String: Any] = [:]) async throws -> T {
        let body = json.isEmpty ? Data("{}".utf8) : try JSONSerialization.data(withJSONObject: json)
        return try await request(path, method: "POST", body: body,
                                 contentType: "application/json")
    }

    /// POST with no decoded response (fire-and-forget control actions).
    public func postVoid(_ path: String, json: [String: Any] = [:]) async throws {
        let _: EmptyResponse = try await post(path, json: json)
    }

    /// DELETE with no decoded response. The backend's delete routes answer `{}` on success and 404 when
    /// the resource is already gone, which `check` turns into `.notFound` for the caller to render.
    public func deleteVoid(_ path: String) async throws {
        let _: EmptyResponse = try await request(path, method: "DELETE", body: nil, contentType: nil)
    }

    /// Raw bytes for downloads — used by the file-transfer layer.
    public func download(relativePath: String) async throws -> (Data, String?) {
        let url = configuration.baseURL.appendingPathComponent(relativePath.saliStripLeadingSlash)
        let (data, resp) = try await authorized(url: url, method: "GET", body: nil, contentType: nil)
        try Self.check(resp, data)
        let ct = (resp as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Type")
        return (data, ct)
    }

    /// Raw upload of file bytes to a task (§10). Filename is passed as a query param, body is the bytes.
    @discardableResult
    public func upload(taskId: String, filename: String, data: Data) async throws -> [String: Any] {
        var comps = URLComponents(url: configuration.apiRoot.appendingPathComponent("tasks/\(taskId)/files"),
                                  resolvingAgainstBaseURL: false)!
        comps.queryItems = [URLQueryItem(name: "filename", value: filename)]
        let (respData, resp) = try await authorized(url: comps.url!, method: "POST",
                                                    body: data, contentType: "application/octet-stream")
        try Self.check(resp, respData)
        return (try? JSONSerialization.jsonObject(with: respData) as? [String: Any]) ?? [:]
    }

    /// Upload file bytes to the ONGOING conversation — no task required. Lands in the conversation
    /// workspace and returns the server `ref` (an absolute path the backend accepts as `image_ref`
    /// on the next message, so Sali can describe/reason over it). Sali is also made aware of the drop.
    @discardableResult
    public func uploadChatFile(filename: String, data: Data) async throws -> String {
        var comps = URLComponents(url: configuration.apiRoot.appendingPathComponent("files"),
                                  resolvingAgainstBaseURL: false)!
        comps.queryItems = [URLQueryItem(name: "filename", value: filename)]
        let (respData, resp) = try await authorized(url: comps.url!, method: "POST",
                                                    body: data, contentType: "application/octet-stream")
        try Self.check(resp, respData)
        let json = (try? JSONSerialization.jsonObject(with: respData) as? [String: Any]) ?? [:]
        // A 2xx with no usable `ref` is a failed upload, not a success — throw so the caller marks the
        // attachment failed and offers retry, rather than posting a turn with an empty image reference.
        guard let ref = json["ref"] as? String, !ref.isEmpty else {
            throw APIError.decoding("upload returned no file reference")
        }
        return ref
    }

    // MARK: - Core request with single-flight refresh

    private func request<T: Decodable>(_ path: String, method: String, query: [String: String] = [:],
                                       body: Data?, contentType: String? = "application/json") async throws -> T {
        var comps = URLComponents(url: configuration.apiRoot.appendingPathComponent(path.saliStripLeadingSlash),
                                  resolvingAgainstBaseURL: false)!
        if !query.isEmpty { comps.queryItems = query.map { URLQueryItem(name: $0.key, value: $0.value) } }
        let url = comps.url!

        var (data, resp) = try await authorized(url: url, method: method, body: body, contentType: contentType)
        if (resp as? HTTPURLResponse)?.statusCode == 401 {
            // one refresh attempt, shared across concurrent callers
            if await refreshIfPossible() {
                (data, resp) = try await authorized(url: url, method: method, body: body, contentType: contentType)
            }
        }
        try Self.check(resp, data)
        if T.self == EmptyResponse.self { return EmptyResponse() as! T }
        do { return try Self.decoder.decode(T.self, from: data) }
        catch { throw APIError.decoding(String(describing: error)) }
    }

    private func authorized(url: URL, method: String, body: Data?, contentType: String?) async throws
        -> (Data, URLResponse) {
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.httpBody = body
        if let contentType { req.setValue(contentType, forHTTPHeaderField: "Content-Type") }
        if let token = tokenStore.accessToken {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        do { return try await session.data(for: req) }
        catch { throw APIError.offline }
    }

    /// Single-flight re-authentication: the first caller runs it; the rest await the same result.
    ///
    /// This is where password auth makes lockout impossible. It re-logs-in with the STORED PASSWORD (not a
    /// refresh token) to mint a brand-new session, so a token's TTL is irrelevant — the password mints
    /// another one on demand. The ONE distinction that matters: a 401 from /auth/login means the password
    /// is wrong or was rotated on the host (`sali change-password`) → this is the only thing that logs the
    /// app out (`onAuthLost`). EVERY other failure — offline, timeout, 5xx, 429 — returns false WITHOUT
    /// clearing the password, so the credential survives and the next request simply tries again. A user
    /// far from the PC with flaky signal can therefore never be stranded.
    private func refreshIfPossible() async -> Bool {
        if let task = refreshTask { return await task.value }
        guard let password = tokenStore.password, !password.isEmpty else {
            onAuthLost?()   // no stored password at all → genuinely cannot recover → show login
            return false
        }
        let task = Task<Bool, Never> { [configuration, tokenStore, session, onAuthLost] in
            let name = UserDefaults.standard.string(forKey: "sali.deviceName") ?? "iPhone"
            var req = URLRequest(url: configuration.apiRoot.appendingPathComponent("auth/login"))
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try? JSONSerialization.data(
                withJSONObject: ["password": password, "name": name, "platform": "ios"])
            do {
                let (data, resp) = try await session.data(for: req)
                guard let http = resp as? HTTPURLResponse else { return false }   // transient — keep password
                if http.statusCode == 401 {
                    // The password is wrong / was rotated. The ONLY path that logs the app out.
                    onAuthLost?()
                    return false
                }
                guard http.statusCode == 200,
                      let sess = try? APIClient.decoder.decode(IssuedSession.self, from: data) else {
                    return false   // 5xx / 429 / malformed — transient, keep the password and retry later
                }
                tokenStore.save(session: sess)
                return true
            } catch {
                return false   // offline / timeout — keep the password, the next request retries
            }
        }
        refreshTask = task
        let ok = await task.value
        refreshTask = nil
        return ok
    }

    /// The host explains its refusals — FastAPI answers `{"detail": "…"}` with a real reason ("unknown
    /// layer 'foo'", "cron expression is invalid", "schedule not found"). Discarding it and showing
    /// "request failed" turned every correctable mistake into a dead end the user could not act on, so
    /// the body is read here and its reason carried into the error.
    private static func detail(_ data: Data?) -> String? {
        guard let data, !data.isEmpty,
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        if let d = obj["detail"] as? String, !d.isEmpty { return d }
        // FastAPI validation errors arrive as a list of {loc, msg, type}.
        if let list = obj["detail"] as? [[String: Any]] {
            let msgs = list.compactMap { $0["msg"] as? String }.filter { !$0.isEmpty }
            if !msgs.isEmpty { return msgs.joined(separator: "; ") }
        }
        return nil
    }

    private static func check(_ resp: URLResponse, _ data: Data? = nil) throws {
        guard let http = resp as? HTTPURLResponse else { throw APIError.offline }
        let reason = detail(data)
        switch http.statusCode {
        case 200...299: return
        case 401: throw APIError.unauthorized
        case 403: throw APIError.forbidden(reason ?? "This device isn't allowed to do that.")
        case 404: throw APIError.notFound
        case 409: throw APIError.conflict(reason ?? "Conflict.")
        case 410: throw APIError.notFound
        case 413: throw APIError.tooLarge
        default:  throw APIError.server(http.statusCode, reason ?? "request failed")
        }
    }
}

public struct EmptyResponse: Decodable, Sendable { public init() {} }

private extension String {
    /// Drop a single leading "/" so a path can be appended to a base URL cleanly.
    var saliStripLeadingSlash: String { hasPrefix("/") ? String(dropFirst()) : self }
}
