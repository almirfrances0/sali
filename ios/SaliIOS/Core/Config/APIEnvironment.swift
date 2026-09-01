import Foundation

/// Where the app talks to Sali. The production endpoint is not hardcoded into feature code — the app
/// resolves the base URL from the selected environment (§32), and the WebSocket URL is derived from it.
/// No secrets or enrollment codes live in the source.
public enum APIEnvironment: String, CaseIterable, Codable, Sendable {
    case development
    case staging
    case production

    /// The REST base, e.g. `https://sali.salieno.com`. Development points at the Kali box on the LAN;
    /// override it in Settings for your own IP (persisted, no recompile needed, §32).
    public var defaultBaseURL: URL {
        switch self {
        case .development: URL(string: "http://127.0.0.1:8080")!
        case .staging:     URL(string: "https://staging.salieno.com")!
        case .production:  URL(string: "https://sali.salieno.com")!
        }
    }

    public var title: String {
        switch self {
        case .development: "Development"
        case .staging:     "Staging"
        case .production:  "Production"
        }
    }
}

/// The resolved connection configuration. `baseURL` may be user-overridden (e.g. a specific LAN IP) while
/// keeping the environment label. WebSocket URL is derived by swapping the scheme and path.
public struct APIConfiguration: Codable, Equatable, Sendable {
    public var environment: APIEnvironment
    public var baseURL: URL

    public init(environment: APIEnvironment = .production, baseURL: URL? = nil) {
        self.environment = environment
        self.baseURL = baseURL ?? environment.defaultBaseURL
    }

    /// `/api/v1` REST root.
    public var apiRoot: URL { baseURL.appendingPathComponent("api/v1") }

    /// `/ws` WebSocket URL, scheme mapped http→ws / https→wss.
    public var webSocketURL: URL {
        var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)!
        components.scheme = (components.scheme == "https") ? "wss" : "ws"
        components.path = "/ws"
        return components.url!
    }

    public var healthzURL: URL { baseURL.appendingPathComponent("healthz") }
}
