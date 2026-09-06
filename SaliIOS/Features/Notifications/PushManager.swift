import Foundation
#if canImport(UIKit)
import UIKit
#endif
#if canImport(UserNotifications)
import UserNotifications
#endif

// MARK: - Push notifications (§11) — the two system calls and the one network call
//
// This file is the seam: `requestAuthorization()`, `registerForRemoteNotifications()`, and the POST that
// hands the resulting token to Sali. It is deliberately stateless, so it never becomes a second source of
// truth for whether push is set up.
//
// The app's half is wired end to end now: the `aps-environment` entitlement is on the target, and
// `SaliAppDelegate` (PushRegistration.swift) receives the token iOS only ever delivers through a
// `UIApplicationDelegate` callback, hexes it with `hexString(from:)`, and submits it here.
//
// Two things are still genuinely absent, and neither is an app setting:
//
//   1. A sender. Sali's server stores this token (`api_device.push_token`) but has no APNs client — there
//      is nothing on the host that ever pushes to it. Until that exists, no banner can arrive FROM Sali;
//      its messages reach the phone over the WebSocket and are kept in Inbox (§6/§10).
//
//   2. Silent pushes. If Sali ever wants to wake the app without a banner, that needs "Remote
//      notifications" under Background Modes plus `content-available: 1` on the payload. Not needed for
//      plain alert pushes, so left undone.

/// Abstraction over the two OS calls push setup needs, so callers (and tests) depend on a protocol instead
/// of `UNUserNotificationCenter` / `UIApplication` directly.
/// `Sendable` because the callers are main-actor types (`PushRegistration`) awaiting calls that run off
/// the main actor — an implementation with mutable state would be a data race, not a design choice.
public protocol PushRegistering: Sendable {
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
    /// `POST /api/v1/devices/{deviceId}/push-token { "token": …, "environment": … }`.
    ///
    /// `environment` describes the APNs environment the TOKEN belongs to, not the Sali server
    /// `APIEnvironment` — the two are unrelated concepts that happen to share vocabulary. The server
    /// accepts only `"sandbox"` or `"production"` (`devices.py: set_push_token` raises on anything else,
    /// and the `api_device` CHECK enforces it), so a debug build's `aps-environment = development`
    /// token must be submitted as `"sandbox"`. `PushRegistration.apsEnvironment` does that mapping.
    ///
    /// Returns whether the server took it. Push is a convenience delivery path layered on the WebSocket
    /// stream (§6), never a control path, so a failure is never thrown at the person — but it is
    /// reported, because "Sali has this device's token" must not be claimed on a request that failed.
    @discardableResult
    public func submitToken(_ hexToken: String, deviceId: String,
                            environment: String, api: APIClient) async -> Bool {
        do {
            try await api.postVoid(
                "devices/\(deviceId)/push-token",
                json: ["token": hexToken, "environment": environment]
            )
            return true
        } catch {
            return false
        }
    }
}
