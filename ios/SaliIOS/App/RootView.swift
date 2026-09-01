import SwiftUI

/// Routes between onboarding (no device authority yet) and the main tabbed control center. Secondary areas
/// (Memory, Learning, Notifications, Settings) live under a "More" tab so the tab bar stays legible on an
/// iPhone 15 Pro (§4 — not every concept belongs in the tab bar).
public struct RootView: View {
    @EnvironmentObject private var appState: AppState
    @EnvironmentObject private var auth: AuthService

    public init() {}

    public var body: some View {
        Group {
            if auth.isEnrolled {
                MainTabView()
            } else {
                OnboardingView()
            }
        }
        .animation(Theme.Motion.gentle, value: auth.isEnrolled)
        .onAppear { if auth.isEnrolled { appState.start() } }
    }
}

struct MainTabView: View {
    @EnvironmentObject private var appState: AppState

    var body: some View {
        TabView(selection: $appState.selectedTab) {
            ChatView()
                .tabItem { Label("Chat", systemImage: "bubble.left.and.bubble.right") }
                .tag(AppTab.chat)

            ActivityView()
                .tabItem { Label("Activity", systemImage: "waveform.path.ecg") }
                .tag(AppTab.activity)

            TasksView()
                .tabItem { Label("Tasks", systemImage: "checklist") }
                .tag(AppTab.tasks)

            LifeView()
                .tabItem { Label("Life", systemImage: "sparkles") }
                .tag(AppTab.life)

            MoreView()
                .tabItem { Label("More", systemImage: "ellipsis.circle") }
                .tag(AppTab.more)
        }
    }
}

/// The "More" hub — deeper areas that don't warrant a primary tab.
struct MoreView: View {
    var body: some View {
        NavigationStack {
            List {
                NavigationLink { SystemView() } label: {
                    Label("System Health", systemImage: "cpu")
                }
                NavigationLink { MemoryView() } label: {
                    Label("Memory & Experiences", systemImage: "brain")
                }
                NavigationLink { LearningView() } label: {
                    Label("Learning & Capabilities", systemImage: "graduationcap")
                }
                NavigationLink { NotificationsView() } label: {
                    Label("Messages from Sali", systemImage: "bell.badge")
                }
                NavigationLink { SettingsView() } label: {
                    Label("Settings & Security", systemImage: "gearshape")
                }
            }
            .navigationTitle("More")
        }
    }
}
