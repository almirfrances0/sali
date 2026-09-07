import Foundation
import SwiftUI
import UIKit
import UserNotifications

// MARK: - The OS half of push (§11)
//
// `PushManager` owns the two system calls and the one network call. This owns the part that cannot be a
// stateless struct: the permission the person granted, the APNs token iOS hands back *asynchronously*
// through the app delegate, and whether Sali's server has accepted it.
//
// Nothing here keeps a permission flag of its own. `authorization` is re-read from
// `UNUserNotificationCenter` whenever the app becomes active, because the switch that actually decides
// this lives in iOS Settings and can be flipped while the app is in the background — an app-side "on"
// would start lying the moment that happened.

/// Push as this device can actually observe it. Read by Settings, written by `SaliAppDelegate`.
@MainActor
public final class PushRegistration: ObservableObject {
    /// One instance, because `UIApplicationDelegate` is constructed by UIKit and cannot be handed
    /// dependencies. The delegate writes here; SwiftUI reads here.
    public static let shared = PushRegistration()

    /// What iOS says right now — the only honest source for whether banners are allowed.
    @Published public private(set) var authorization: UNAuthorizationStatus = .notDetermined
    /// The APNs token, hex-encoded, once iOS has one. Nil until `didRegister(deviceToken:)` fires.
    @Published public private(set) var token: String?
    /// The token Sali's server accepted, so "registered" is never claimed on the strength of a POST we
    /// didn't see succeed. PERSISTED to UserDefaults so a re-launch doesn't re-POST the same token
    /// (the previous "at most once per token" claim was true only within a single process lifetime).
    @Published public private(set) var acceptedToken: String? {
        didSet {
            if let acceptedToken {
                UserDefaults.standard.set(acceptedToken, forKey: Self._acceptedTokenKey)
            } else {
                UserDefaults.standard.removeObject(forKey: Self._acceptedTokenKey)
            }
        }
    }
    private static let _acceptedTokenKey = "sali.push.acceptedToken"

    /// The (deviceId, token) pair the backend accepted — durable across launches. Used by the
    /// submit gate so a sign-out + re-enroll with a fresh deviceId re-POSTs instead of skipping.
    @Published public private(set) var acceptedPair: String? {
        didSet {
            if let acceptedPair {
                UserDefaults.standard.set(acceptedPair, forKey: Self._acceptedPairKey)
            } else {
                UserDefaults.standard.removeObject(forKey: Self._acceptedPairKey)
            }
        }
    }
    private static let _acceptedPairKey = "sali.push.acceptedPair"
    /// Why the OS or the server refused, in their own words. Shown; never invented.
    @Published public private(set) var failure: String?
    /// A permission request or a token submission is in flight.
    @Published public private(set) var isWorking = false

    private let registrar: PushRegistering
    private let manager = PushManager()

    public init(registrar: PushRegistering = PushManager()) {
        self.registrar = registrar
        // Restore the persisted acceptedToken so a cold start with the same APNs token doesn't
        // re-POST /devices/{id}/push-token on every foreground.
        self.acceptedToken = UserDefaults.standard.string(forKey: Self._acceptedTokenKey)
        self.acceptedPair = UserDefaults.standard.string(forKey: Self._acceptedPairKey)
    }

    /// Which APNs environment this build's tokens belong to. Sali's `api_device` row accepts only
    /// `sandbox` or `production` (`devices.py: set_push_token`), and a debug build signed with
    /// `aps-environment = development` produces a SANDBOX token — "development" on the wire is a 500,
    /// not a synonym.
    /// Which APNs environment this build's token actually belongs to.
    ///
    /// Read from the `aps-environment` entitlement signed into the app, NOT from the build
    /// configuration. Those are different facts, and when they disagreed push broke completely and
    /// silently: the entitlement said `development` for Debug AND Release, so a Release build held a
    /// sandbox token while this property claimed "production". The backend picks the APNs host from
    /// that string, Apple answered BadDeviceToken, the server pruned the token as dead, and the app
    /// never re-registered. Reading the entitlement means the two can no longer disagree — whatever
    /// the entitlement says, that is what we report.
    public static var apsEnvironment: String {
        if let fromProfile = entitlementAPSEnvironment {
            return fromProfile == "production" ? "production" : "sandbox"
        }
        // No embedded profile: the simulator, or a build installed without one. Fall back to the
        // build flag, which is the best guess available and matches the old behaviour.
        #if DEBUG
        return "sandbox"
        #else
        return "production"
        #endif
    }

    /// `aps-environment` from the embedded provisioning profile, or nil if it cannot be read.
    ///
    /// The profile is CMS-wrapped, so the plist is extracted by locating the XML between its
    /// `<?xml` header and closing `</plist>` rather than by parsing the signature.
    static let entitlementAPSEnvironment: String? = {
        guard let url = Bundle.main.url(forResource: "embedded", withExtension: "mobileprovision"),
              let data = try? Data(contentsOf: url),
              let text = String(data: data, encoding: .isoLatin1),
              let start = text.range(of: "<?xml"),
              let end = text.range(of: "</plist>")
        else { return nil }
        let plist = String(text[start.lowerBound..<end.upperBound])
        guard let plistData = plist.data(using: .isoLatin1),
              let root = try? PropertyListSerialization.propertyList(
                  from: plistData, options: [], format: nil) as? [String: Any],
              let entitlements = root["Entitlements"] as? [String: Any]
        else { return nil }
        return entitlements["aps-environment"] as? String
    }()

    // MARK: - What to say about it

    /// Derived, never stored — so the row can't disagree with iOS.
    public enum State: Equatable {
        /// The system prompt has never been raised. The only state where asking is possible.
        case notAsked
        /// The person said no, or turned it off later. Only iOS Settings can undo this.
        case denied
        /// Allowed, but APNs hasn't handed us a token yet (or this build can't get one).
        case allowedWithoutToken
        /// We hold a token; Sali's server hasn't taken it yet.
        case tokenPending
        /// Sali has this device's token — everything the app can do is done.
        case registered
        /// Registration failed and iOS or the server told us why.
        case failed(String)
    }

    public var state: State {
        if let failure { return .failed(failure) }
        switch authorization {
        case .notDetermined:
            return .notAsked
        case .denied:
            return .denied
        default:
            guard let token else { return .allowedWithoutToken }
            return acceptedToken == token ? .registered : .tokenPending
        }
    }

    // MARK: - Asking

    /// Ask iOS what the person has actually decided, and re-register when we're allowed but holding no
    /// token (iOS drops the APNs registration on a restore, and permission outlives it).
    public func refreshAuthorization() async {
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        authorization = settings.authorizationStatus
        if settings.authorizationStatus != .denied, settings.authorizationStatus != .notDetermined,
           token == nil {
            registrar.registerForRemoteNotifications()
        }
    }

    /// The one place the system prompt is raised. Deliberately returns nothing: the answer is
    /// `authorization`, read back from iOS afterwards, so a "granted" we couldn't verify is never
    /// rendered as one. iOS shows this prompt once per install — after that, only its own Settings can
    /// change the answer, which is what `openSystemSettings()` is for.
    public func requestPermission() async {
        guard !isWorking else { return }
        isWorking = true
        failure = nil
        if await registrar.requestAuthorization() {
            registrar.registerForRemoteNotifications()
        }
        await refreshAuthorization()
        isWorking = false
    }

    /// Send the person to Sali's page in iOS Settings, where the notification switch actually lives.
    public func openSystemSettings() {
        guard let url = URL(string: UIApplication.openSettingsURLString) else { return }
        UIApplication.shared.open(url)
    }

    // MARK: - What the app delegate reports

    /// APNs answered. Called on the main thread from `SaliAppDelegate`.
    func didRegister(deviceToken: Data) {
        token = PushManager.hexString(from: deviceToken)
        failure = nil
    }

    /// APNs refused. Keeping the reason is the difference between "push is off" and "push is broken".
    func didFailToRegister(error: Error) {
        token = nil
        acceptedToken = nil
        failure = error.localizedDescription
    }

    // MARK: - Handing the token to Sali

    /// `POST /api/v1/devices/{deviceId}/push-token`, at most once per token. A token iOS reissues
    /// replaces the old one; the same token is never posted twice, so a foregrounding costs no request.
    ///
    /// A transient POST failure (backend restart, network flap, VPN blip) does NOT null
    /// `acceptedToken` — the previous version did, which permanently unregistered the device
    /// until APNs rotated the token OR the user relaunched. Now `acceptedToken` stays at its
    /// last-good value, `failure` records the reason, and the next foreground / scenePhase
    /// active will retry via `retryIfPending(_:)`.
    public func submitTokenIfNeeded(api: APIClient, deviceId: String?) async {
        // Gate on the (token, deviceId) PAIR — not token alone. On a sign-out + re-enroll, the
        // deviceId changes but APNs re-issues the SAME token for the same install; a token-only
        // gate would then skip the POST, leaving the new api_device row with an empty push_token
        // and every server push silently no-op'ing. The accepted-pair persists across launches;
        // resetForReenrollment() wipes it explicitly on sign-out to force a fresh POST.
        guard let token, let deviceId, !deviceId.isEmpty, !isWorking else { return }
        let pair = Self.pairKey(token: token, deviceId: deviceId)
        if acceptedPair == pair { return }
        isWorking = true
        if await manager.submitToken(token, deviceId: deviceId,
                                     environment: Self.apsEnvironment, api: api) {
            acceptedToken = token
            acceptedPair = pair
            failure = nil
        } else {
            failure = "Sali didn't accept this device's push token."
        }
        isWorking = false
    }

    /// Called by AppState.signOut() so a subsequent enrollment (new deviceId, same APNs token)
    /// re-POSTs the token against the new api_device row instead of skipping via the accepted-
    /// token gate. Without this, pushes to the newly-enrolled device were silently dropped.
    public func resetForReenrollment() {
        acceptedToken = nil
        acceptedPair = nil
        failure = nil
    }

    /// A stable dedup key over the (deviceId, token) pair the backend row is scoped to.
    private static func pairKey(token: String, deviceId: String) -> String {
        "\(deviceId)|\(token)"
    }

    /// Called from SaliApp on every scenePhase transition to active AND after enrollment lands
    /// (so a token that arrived BEFORE enrollment — the classic race where APNs delivers before
    /// the user has entered the pairing code — retries once the deviceId is known). Idempotent.
    public func retryIfPending(api: APIClient, deviceId: String?) async {
        // Nothing to do if we already sent the current token, or if we don't have one yet.
        guard let token, token != acceptedToken else { return }
        await submitTokenIfNeeded(api: api, deviceId: deviceId)
    }
}

// MARK: - The delegate iOS needs

/// The APNs token exists only as an argument to a `UIApplicationDelegate` callback — there is no
/// synchronous way to ask for it — so a SwiftUI app that wants push must adopt a delegate. This is that
/// delegate, and it does nothing else: it forwards the token (or the failure) to `PushRegistration` and
/// tells iOS to actually show a notification that arrives while the app is open.
@MainActor
public final class SaliAppDelegate: NSObject, UIApplicationDelegate {
    public func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        return true
    }

    public func application(_ application: UIApplication,
                            didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        PushRegistration.shared.didRegister(deviceToken: deviceToken)
    }

    public func application(_ application: UIApplication,
                            didFailToRegisterForRemoteNotificationsWithError error: Error) {
        PushRegistration.shared.didFailToRegister(error: error)
    }
}

extension SaliAppDelegate: UNUserNotificationCenterDelegate {
    /// Without this, iOS silently swallows a push that lands while the app is in the foreground.
    /// BUT: unconditionally presenting the banner while the WebSocket has just delivered the
    /// same event in-app is double-noise ("banner + chat bubble for the message you're reading").
    /// Never interrupt an OPEN app. This method is, by contract, only ever called while Sali is in the
    /// FOREGROUND — so the person can already see the message land in the transcript, and a banner or sound
    /// over the open app is exactly the noise Almir asked to remove ("if opened, no need"). Returning `[]`
    /// suppresses it on every tab. Backgrounded/closed pushes never reach this method, so their normal
    /// lock-screen alert is untouched — that is the "if the app is closed, I get a notification" half.
    public nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([])
    }

    /// Push TAP handler — Almir §7 explicit requirement: "tapping notification → correct
    /// chat/conversation → correct message". The backend build_payload (push.py:151-165) puts
    /// `sali_event_type` and `sali_task_id` in userInfo; we route them into AppState.open(...)
    /// via a shared PushDeepLink relay (AppState observes it from its @Published property).
    public nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        let userInfo = response.notification.request.content.userInfo
        let eventType = userInfo["sali_event_type"] as? String
        let taskID = userInfo["sali_task_id"] as? String
        let eventID = userInfo["sali_event_id"] as? String
        Task { @MainActor in
            PushDeepLink.shared.pending = PushDeepLink.Pending(
                eventType: eventType, taskID: taskID, eventID: eventID)
            completionHandler()
        }
    }
}

/// Bridges push notification taps + foreground context into AppState. Kept as a global (like
/// PushRegistration.shared) because UIApplicationDelegate cannot be handed dependencies.
///
/// AppState reads two things from here:
/// * `pending` — set by the didReceive tap handler; AppState.consumePendingDeepLink() drains it
///   on the next runloop pass and calls open(subjectOf:taskID:).
/// * `currentTab` / `socketLive` — written by AppState / WebSocketClient so the willPresent
///   handler can suppress in-foreground banners for messages the user is already seeing.
@MainActor
public final class PushDeepLink: ObservableObject {
    public static let shared = PushDeepLink()

    public struct Pending: Equatable {
        public let eventType: String?
        public let taskID: String?
        public let eventID: String?
    }

    @Published public var pending: Pending?
    @Published public var currentTab: AppTab = .chat
    @Published public var socketLive: Bool = false

    private init() {}
}
