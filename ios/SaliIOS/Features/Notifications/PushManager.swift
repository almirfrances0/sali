import Foundation
#if canImport(UIKit)
import UIKit
#endif
#if canImport(UserNotifications)
import UserNotifications
#endif

// MARK: - Push notifications (§11) — interface only, honestly incomplete
//
// This file gives Sali's iPhone app a clean, testable seam for APNs push. It does NOT fake end-to-end
// delivery: `requestAuthorization()` and `registerForRemoteNotifications()` below are real, working system
// calls today — but the plumbing that turns "iOS agreed to register" into "the backend has a usable token"
// cannot be finished from source files alone. What remains, and why:
//
//   1. Entitlement — add "Push Notifications" under the app target's Signing & Capabilities in Xcode. This
//      writes `aps-environment` (`development` in debug, `production` for a distribution build) into the
//      app's entitlements file, which is generated/managed by Xcode, not something a source file can add.
//
//   2. App delegate hookup — adopt `UIApplicationDelegate` (typically via `@UIApplicationDelegateAdaptor`
//      on the SwiftUI `App`) and implement:
//        func application(_ application: UIApplication,
//                          didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data)
//        func application(_ application: UIApplication,
//                          didFailToRegisterForRemoteNotificationsWithError error: Error)
//      iOS calls the first with the raw APNs device token only after `registerForRemoteNotifications()`
//      succeeds — there is no synchronous way to get the token, so this file cannot return one directly.
//
//   3. Data → hex — the raw token is meaningless to a server as `Data`; convert it with
//      `PushManager.hexString(from:)` (provided below) inside that delegate method.
//
//   4. Background modes — if Sali ever wants silent pushes (wake the app to refresh state without a
//      banner), add "Remote notifications" under Signing & Capabilities → Background Modes, and the server
//      would need to send `content-available: 1` on those payloads. Not needed for the first cut (plain
//      alert pushes for agent messages), so left undone.
//
//   5. Once (1)-(3) exist, call `PushManager().submitToken(...)` from the delegate method with the hex
//      token, this device's id, and `"development"`/`"production"` matching the entitlement. That method IS
//      wired below — it really calls `POST /api/v1/devices/{deviceId}/push-token` (API_REFERENCE §5/§11).
//
// None of this needs new architecture — only an Xcode project with signing capabilities and an enrolled
// Apple Developer Program membership, neither of which exists in this checkout.

/// Abstraction over the two OS calls push setup needs, so callers (and tests) depend on a protocol instead
/// of `UNUserNotificationCenter` / `UIApplication` directly.
public protocol PushRegistering {
    /// Ask the person for permission to show alerts/sounds/badges. Returns whether they granted it.
    func requestAuthorization() async -> Bool

    /// Ask iOS to contact APNs for a device token. The token itself is NOT returned here — it arrives later,
    /// asynchronously, in `UIApplicationDelegate.application(_:didRegisterForRemoteNotificationsWithDeviceToken:)`
    /// (see the file-level doc comment above for the app-target wiring that delivery still needs).
    func registerForRemoteNotifications()
}

/// The real `PushRegistering` for iOS. Stateless by design — it wraps two OS calls and one network call,
/// nothing more, so it never becomes a second source of truth for whether push is set up (that lives in
/// `DeviceInfo.hasPush` / `pushEnvironment`, read from the backend).
public struct PushManager: PushRegistering {
    public init() {}

    public func requestAuthorization() async -> Bool {
        #if canImport(UserNotifications)
        do {
            return try await UNUserNotificationCenter.current()
                .requestAuthorization(options: [.alert, .badge, .sound])
        } catch {
            return false
        }
        #else
        return false
        #endif
    }

    public func registerForRemoteNotifications() {
        #if canImport(UIKit)
        Task { @MainActor in
            UIApplication.shared.registerForRemoteNotifications()
        }
        #endif
    }

    /// Converts the raw APNs device token (handed to
    /// `didRegisterForRemoteNotificationsWithDeviceToken:`) into the lowercase hex string the backend
    /// expects as `token` in `POST /api/v1/devices/{deviceId}/push-token`.
    public static func hexString(from deviceToken: Data) -> String {
        deviceToken.map { String(format: "%02x", $0) }.joined()
    }

    /// Registers (or refreshes) this device's push token with the backend — API_REFERENCE §5/§11:
    /// `POST /api/v1/devices/{deviceId}/push-token { "token": …, "environment": … }`. `environment` should
    /// match the app's `aps-environment` entitlement (`"development"` or `"production"`), not the Sali
    /// server `APIEnvironment` — the two are unrelated concepts that happen to share vocabulary.
    ///
    /// Best-effort by design: push is a convenience delivery path layered on top of the WebSocket stream
    /// (§6), never a control path, so a failure here degrades silently to "no push this session" rather
    /// than surfacing a blocking error to the person.
    public func submitToken(_ hexToken: String, deviceId: String, environment: String, api: APIClient) async {
        do {
            try await api.postVoid(
                "devices/\(deviceId)/push-token",
                json: ["token": hexToken, "environment": environment]
            )
        } catch {
            // Intentionally swallowed — see doc comment above.
        }
    }
}
