import SwiftUI

/// Entry point. Injects the composition root and shows onboarding until the device is enrolled, then the
/// main control center. When this package is dropped into an Xcode App target, this `@main` becomes the app.
@main
public struct SaliApp: App {
    @StateObject private var appState = AppState()
    /// Push needs a `UIApplicationDelegate`: the APNs token is only ever delivered as an argument to one
    /// of its callbacks (see PushRegistration.swift). Adopting it here changes nothing else about the app.
    @UIApplicationDelegateAdaptor(SaliAppDelegate.self) private var appDelegate
    @StateObject private var push = PushRegistration.shared
    @Environment(\.scenePhase) private var scenePhase

    public init() {}

    public var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(appState)
                .environmentObject(appState.auth)
                .environmentObject(appState.ws)
                .environmentObject(push)
                .tint(Theme.Colors.accent)
                // Read from iOS rather than remembered: the notification switch lives in iOS Settings and
                // can be flipped while this app is in the background.
                .task { await push.refreshAuthorization() }
                // Runs on launch and again each time iOS issues a different token. Submitting the same
                // token twice is a no-op inside, so a foregrounding costs no request.
                .task(id: push.token) {
                    await push.submitTokenIfNeeded(api: appState.api,
                                                   deviceId: appState.tokenStore.deviceId)
                }
                .onChange(of: scenePhase) { _, phase in
                    // §30/§40: drop the socket when backgrounded, recover the gap on foreground (subscribe
                    // sends after_seq so no events are missed).
                    switch phase {
                    case .active:
                        if appState.isEnrolled { appState.start() }
                        Task { await push.refreshAuthorization() }
                        // Retry a pending token submission — covers the case where the token
                        // POST failed transiently on the previous foreground OR where the token
                        // arrived from APNs BEFORE the user had finished enrollment (in which
                        // case deviceId was nil when the .task(id:push.token) below first fired
                        // and it never re-triggered — see PushRegistration.retryIfPending).
                        Task {
                            await push.retryIfPending(api: appState.api,
                                                      deviceId: appState.tokenStore.deviceId)
                        }
                    case .background: appState.stop()
                    default: break
                    }
                }
                // Also retry token submission every time login state flips (post-login). Uses
                // appState.auth.isLoggedIn as the trigger since deviceId itself is not @Published.
                .onChange(of: appState.auth.isLoggedIn) { _, loggedIn in
                    if loggedIn {
                        Task {
                            await push.retryIfPending(api: appState.api,
                                                      deviceId: appState.tokenStore.deviceId)
                        }
                    }
                }
        }
    }
}
