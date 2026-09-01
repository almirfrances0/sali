import Foundation
import SwiftUI

// Settings & Security — the connection, this device's identity, and an honest account of what protects
// the user (§5/§17/§27/§32). Nothing here fakes capability that doesn't exist yet (Notifications), and the
// one destructive action (sign out) is gated behind a deliberate confirmation, never a single tap.

@MainActor
final class SettingsViewModel: ObservableObject {
    enum ConnectionTestResult: Equatable {
        case idle, testing, ok, unreachable(String)
    }

    @Published var selectedEnvironment: APIEnvironment = .production
    @Published var baseURLText: String = ""
    @Published var connectionTest: ConnectionTestResult = .idle

    private var configured = false

    /// Seed the editable fields from the app's live configuration exactly once — `@EnvironmentObject`
    /// values aren't available at `init()`, so this runs from `.task` on first appearance instead.
    func configureIfNeeded(from configuration: APIConfiguration) {
        guard !configured else { return }
        configured = true
        selectedEnvironment = configuration.environment
        baseURLText = configuration.baseURL.absoluteString
    }

    func buildConfiguration() -> APIConfiguration? {
        let trimmed = baseURLText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URL(string: trimmed), url.scheme != nil, url.host != nil else { return nil }
        return APIConfiguration(environment: selectedEnvironment, baseURL: url)
    }

    func testConnection(healthzURL: URL) async {
        connectionTest = .testing
        var request = URLRequest(url: healthzURL)
        request.timeoutInterval = 8
        do {
            let (_, response) = try await URLSession.shared.data(for: request)
            if let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) {
                connectionTest = .ok
            } else {
                connectionTest = .unreachable("Sali answered, but not with success.")
            }
        } catch {
            connectionTest = .unreachable("Couldn't reach Sali at that address.")
        }
    }
}

struct SettingsView: View {
    @EnvironmentObject var appState: AppState
    @EnvironmentObject var auth: AuthService
    @StateObject private var viewModel = SettingsViewModel()

    @State private var showSaveConfirmation = false
    @State private var showSignOutConfirmation = false
    @State private var notificationsToggle = false

    // Pushed as a NavigationLink destination from MoreView, so — like NotificationsView — this does not
    // open its own NavigationStack; it shares the one already on screen (which is also what lets
    // `securitySection`'s NavigationLink to DevicesView push correctly).
    var body: some View {
        List {
            connectionSection
            deviceSection
            securitySection
            notificationsSection
            aboutSection
        }
        .listStyle(.insetGrouped)
        .navigationTitle("Settings")
        .task { viewModel.configureIfNeeded(from: appState.configuration) }
        .sheet(isPresented: $showSaveConfirmation) {
            if let pending = viewModel.buildConfiguration() {
                NaturalConfirmationSheet(
                    title: "Reconnect Sali",
                    explanation: "This points the app at \(pending.baseURL.absoluteString) (\(pending.environment.title)) and reconnects the live session. Your device stays paired — this only changes where the app looks for Sali.",
                    confirmLabel: "Save & reconnect",
                    isDestructive: false
                ) {
                    appState.applyConfiguration(pending)
                }
            }
        }
        .sheet(isPresented: $showSignOutConfirmation) {
            NaturalConfirmationSheet(
                title: "Sign out",
                explanation: "This removes Sali's credentials from this iPhone only. The device pairing itself stays valid until an owner revokes it from Settings → Devices — sign out is local, revocation is what actually cuts off access.",
                confirmLabel: "Sign out",
                isDestructive: true
            ) {
                appState.signOut()
            }
        }
    }

    // MARK: - Connection

    private var connectionSection: some View {
        Section {
            Picker("Environment", selection: $viewModel.selectedEnvironment) {
                ForEach(APIEnvironment.allCases, id: \.self) { env in
                    Text(env.title).tag(env)
                }
            }
            TextField("Base URL", text: $viewModel.baseURLText)
                .keyboardType(.URL)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .font(Theme.Typography.mono)

            Button {
                showSaveConfirmation = true
            } label: {
                Text("Save & reconnect").frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .tint(Theme.Colors.accent)
            .disabled(viewModel.buildConfiguration() == nil)

            if viewModel.buildConfiguration() == nil && !viewModel.baseURLText.isEmpty {
                Label("That doesn't look like a valid address.", systemImage: "exclamationmark.circle")
                    .font(Theme.Typography.caption).foregroundStyle(Theme.Colors.warn)
            }

            HStack {
                Text("Live status")
                Spacer()
                ConnectionBadge(appState.ws.state)
            }

            testConnectionRow
        } header: {
            Text("Connection")
        } footer: {
            Text("Development points at your Kali box on the LAN; Production goes through the Cloudflare Tunnel. Changing this never bypasses Sali's own authentication.")
        }
    }

    private var testConnectionRow: some View {
        HStack {
            Button("Test connection") {
                Task { await viewModel.testConnection(healthzURL: appState.configuration.healthzURL) }
            }
            .disabled(viewModel.connectionTest == .testing)
            Spacer()
            testResultView
        }
    }

    @ViewBuilder private var testResultView: some View {
        switch viewModel.connectionTest {
        case .idle:
            EmptyView()
        case .testing:
            ProgressView()
        case .ok:
            Label("Reachable", systemImage: "checkmark.circle.fill")
                .font(Theme.Typography.caption).foregroundStyle(Theme.Colors.ok)
        case .unreachable(let message):
            Label(message, systemImage: "wifi.exclamationmark")
                .font(Theme.Typography.caption).foregroundStyle(Theme.Colors.danger)
        }
    }

    // MARK: - This device

    private var deviceSection: some View {
        Section {
            LabeledContent("Name") {
                Text(auth.deviceName.isEmpty ? "This iPhone" : auth.deviceName)
            }
            LabeledContent("Role") {
                StatusPill(appState.role.label, color: roleColor)
            }
            Button(role: .destructive) {
                showSignOutConfirmation = true
            } label: {
                Text("Sign out (this device)")
            }
        } header: {
            Text("This device")
        } footer: {
            Text("Signing out only clears this iPhone's local credentials. To cut off a device remotely — including a lost or stolen one — an owner revokes it from Settings → Devices, which ends its sessions immediately even while offline.")
        }
    }

    private var roleColor: Color {
        switch appState.role {
        case .owner: Theme.Colors.accent
        case .controller: Theme.Colors.ok
        case .observer: Theme.Colors.idle
        }
    }

    // MARK: - Security

    private var securitySection: some View {
        Section("Security") {
            NavigationLink {
                DevicesView()
            } label: {
                Label("Devices", systemImage: "iphone.and.arrow.forward")
            }
        }
    }

    // MARK: - Notifications

    private var notificationsSection: some View {
        Section {
            Toggle("Push notifications", isOn: $notificationsToggle)
                .disabled(true)
                .tint(Theme.Colors.accent)
        } header: {
            Text("Notifications")
        } footer: {
            Text("Push needs an Apple Developer account: the Push Notifications entitlement added in Xcode, plus an app delegate to receive the APNs device token. Sali's side (PushManager, and the server's push-token endpoint) is already built and ready — this switches on the moment that signing step happens, so it stays off now rather than pretending to work.")
        }
    }

    // MARK: - About

    private var aboutSection: some View {
        Section {
            LabeledContent("Version") { Text(appVersion) }
            Text("One Sali, one conversation. This app is a window into the same Sali instance you talk to on the terminal — never a second assistant.")
                .font(Theme.Typography.caption)
                .foregroundStyle(Theme.Colors.secondaryText)
            VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                Text("Security").font(Theme.Typography.caption.weight(.semibold))
                Label("Credentials live only in the iOS Keychain", systemImage: "key.fill")
                Label("Cloudflare Tunnel + Sali's own auth — defense in depth", systemImage: "lock.shield")
                Label("Secrets are never shown in plaintext or logged", systemImage: "eye.slash")
            }
            .font(Theme.Typography.caption)
            .foregroundStyle(Theme.Colors.secondaryText)
        } header: {
            Text("About")
        }
    }

    private var appVersion: String {
        let short = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String
        let build = Bundle.main.infoDictionary?["CFBundleVersion"] as? String
        switch (short, build) {
        case let (.some(s), .some(b)): return "\(s) (\(b))"
        case let (.some(s), .none): return s
        default: return "—"
        }
    }
}
