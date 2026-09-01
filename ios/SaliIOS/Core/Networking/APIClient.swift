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

    public init(tokenStore: TokenStore, configuration: APIConfiguration, session: URLSession = .shared) {
        self.tokenStore = tokenStore
        self.configuration = configuration
        self.session = session
    }

    public func updateConfiguration(_ cfg: APIConfiguration) { self.configuration = cfg }
    public func currentConfiguration() -> APIConfiguration { configuration }
    public func setOnAuthLost(_ handler: @escaping @Sendable () -> Void) { self.onAuthLost = handler }

    static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.dateDecodingStrategy = .custom { decoder in
            let s = try decoder.singleValueContainer().decode(String.self)
            let iso = ISO8601DateFormatter()
            iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            if let date = iso.date(from: s) { return date }
            iso.formatOptions = [.withInternetDateTime]
            if let date = iso.date(from: s) { return date }
            throw DecodingError.dataCorruptedError(in: try decoder.singleValueContainer(),
                                                   debugDescription: "bad date: \(s)")
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

    /// Raw bytes for downloads — used by the file-transfer layer.
    public func download(relativePath: String) async throws -> (Data, String?) {
        let url = configuration.baseURL.appendingPathComponent(relativePath.saliStripLeadingSlash)
        let (data, resp) = try await authorized(url: url, method: "GET", body: nil, contentType: nil)
        try Self.check(resp)
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
        try Self.check(resp)
        return (try? JSONSerialization.jsonObject(with: respData) as? [String: Any]) ?? [:]
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
        try Self.check(resp)
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

    /// Single-flight refresh: the first caller runs it; the rest await the same result.
    private func refreshIfPossible() async -> Bool {
        if let task = refreshTask { return await task.value }
        guard let refresh = tokenStore.refreshToken else { onAuthLost?(); return false }
        let task = Task<Bool, Never> { [configuration, tokenStore, session] in
            var req = URLRequest(url: configuration.apiRoot.appendingPathComponent("auth/refresh"))
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try? JSONSerialization.data(withJSONObject: ["refresh_token": refresh])
            guard let (data, resp) = try? await session.data(for: req),
                  (resp as? HTTPURLResponse)?.statusCode == 200,
                  let sess = try? APIClient.decoder.decode(IssuedSession.self, from: data) else {
                return false
            }
            tokenStore.save(session: sess)
            return true
        }
        refreshTask = task
        let ok = await task.value
        refreshTask = nil
        if !ok { onAuthLost?() }
        return ok
    }

    private static func check(_ resp: URLResponse) throws {
        guard let http = resp as? HTTPURLResponse else { throw APIError.offline }
        switch http.statusCode {
        case 200...299: return
        case 401: throw APIError.unauthorized
        case 403: throw APIError.forbidden("This device isn't allowed to do that.")
        case 404: throw APIError.notFound
        case 409: throw APIError.conflict("Conflict.")
        case 410: throw APIError.notFound
        case 413: throw APIError.tooLarge
        default:  throw APIError.server(http.statusCode, "request failed")
        }
    }
}

public struct EmptyResponse: Decodable, Sendable { public init() {} }

private extension String {
    /// Drop a single leading "/" so a path can be appended to a base URL cleanly.
    var saliStripLeadingSlash: String { hasPrefix("/") ? String(dropFirst()) : self }
}
