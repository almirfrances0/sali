import Foundation

/// Typed API errors so the UI can render meaningful states (§30/§37) instead of a spinner-forever.
public enum APIError: Error, LocalizedError, Sendable {
    case unauthorized                 // 401 — needs refresh, or (after refresh) re-enrollment
    case forbidden(String)            // 403 — authenticated but not allowed (observer/role)
    case notFound
    case conflict(String)
    case tooLarge
    case server(Int, String)          // other non-2xx with a detail message
    case offline                      // transport failure / tunnel unreachable
    case decoding(String)
    case notEnrolled                  // no credentials yet — go to onboarding

    public var errorDescription: String? {
        switch self {
        case .unauthorized:        "Your session expired. Reconnecting…"
        case .forbidden(let m):    m
        case .notFound:            "Not found."
        case .conflict(let m):     m
        case .tooLarge:            "That's too large to send."
        case .server(let c, let m):"Server error (\(c)): \(m)"
        case .offline:             "Can't reach Sali. Check your connection."
        case .decoding(let m):     "Unexpected response: \(m)"
        case .notEnrolled:         "This device isn't paired with Sali yet."
        }
    }

    public var isAuthFailure: Bool {
        if case .unauthorized = self { return true }
        if case .notEnrolled = self { return true }
        return false
    }
}
