import SwiftUI

/// Entry point. Injects the composition root and shows onboarding until the device is enrolled, then the
/// main control center. When this package is dropped into an Xcode App target, this `@main` becomes the app.
@main
public struct SaliApp: App {
    @StateObject private var appState = AppState()
    @Environment(\.scenePhase) private var scenePhase

    public init() {}

    public var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(appState)
                .environmentObject(appState.auth)
                .environmentObject(appState.ws)
                .tint(Theme.Colors.accent)
                .onChange(of: scenePhase) { _, phase in
                    // §30/§40: drop the socket when backgrounded, recover the gap on foreground (subscribe
                    // sends after_seq so no events are missed).
                    switch phase {
                    case .active:     if appState.isEnrolled { appState.start() }
                    case .background: appState.stop()
                    default: break
                    }
                }
        }
    }
}
