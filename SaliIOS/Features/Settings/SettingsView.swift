import Foundation
import SwiftUI

// Settings & Security — the connection, this device's identity, and an honest account of what protects
// the user (§5/§17/§27/§32).
//
// Two rules this screen is held to, because it is the screen where a lie is most expensive:
//
// 1. **Every control is real.** There used to be a `Toggle("Push notifications").disabled(true)` here with
//    nothing behind it — a switch that cannot move is not an honest way to say "not built yet", it just
//    reads as broken. It is now a status row that states what works today (Sali's messages are kept in
//    Inbox and survive relaunch), what doesn't (banners while the app is closed), and exactly what is
//    missing. Nothing on this screen is a switch unless flipping it does something.
//
// 2. **The screen never claims a state it hasn't checked.** "Test connection" tests the address *in the
//    field*, not the one that happens to be saved — it used to always probe the saved host, so typing a new
//    LAN IP and pressing Test cheerfully reported the OLD server was reachable. And the result is cleared
//    the moment the address changes, so a stale "Reachable" can never sit next to an address it never saw.
//
// The one destructive action (sign out) stays gated behind a deliberate confirmation, never a single tap.

@MainActor
final class SettingsViewModel: ObservableObject {
    enum ConnectionTestResult: Equatable {
        case idle, testing
        /// Both outcomes carry the host they describe, so the row can never attribute a result to an
        /// address it didn't probe.
        case ok(String)
        case unreachable(String, String)
    }

    @Published var selectedEnvironment: APIEnvironment = .production {
        didSet { if selectedEnvironment != oldValue { environmentChanged(from: oldValue) } }
    }
    @Published var baseURLText: String = "" {
        didSet { if baseURLText != oldValue { connectionTest = .idle } }
    }
    @Published var connectionTest: ConnectionTestResult = .idle

    private var configured = false

    /// Seed the editable fields from the app's live configuration exactly once — `@EnvironmentObject`
    /// values aren't available at `init()`, so this runs from `.task` on first appearance instead.
    func configureIfNeeded(from configuration: APIConfiguration) {
        guard !configured else { return }
        configured = true
        reseed(from: configuration)
    }

    /// Adopt a configuration the app changed elsewhere (Onboarding's Advanced panel, a different device
    /// applying one), so the field can never show an address the app stopped using.
    func reseed(from configuration: APIConfiguration) {
        selectedEnvironment = configuration.environment
        baseURLText = configuration.baseURL.absoluteString
        connectionTest = .idle
    }

    /// Switching environment retargets the URL only when the field is still that environment's default —
    /// a hand-typed LAN address survives the switch, but the field never silently keeps pointing at
    /// production after the person selects Development.
    private func environmentChanged(from previous: APIEnvironment) {
        let wasDefault = baseURLText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            || baseURLText == previous.defaultBaseURL.absoluteString
        if wasDefault { baseURLText = selectedEnvironment.defaultBaseURL.absoluteString }
        connectionTest = .idle
    }

    /// `URL(string: "hello")` succeeds — a base URL is only usable with a scheme *and* a host.
    func buildConfiguration() -> APIConfiguration? {
        let trimmed = baseURLText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty,
              let url = URL(string: trimmed),
              let scheme = url.scheme?.lowercased(), scheme == "http" || scheme == "https",
              let host = url.host, !host.isEmpty else { return nil }
        return APIConfiguration(environment: selectedEnvironment, baseURL: url)
    }

    var addressLooksWrong: Bool {
        !baseURLText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && buildConfiguration() == nil
    }

    /// Is there an edit that hasn't been applied yet? Drives both the "Pending" marker and whether Save is
    /// worth offering at all — re-applying the identical configuration would drop and rebuild a healthy
    /// socket for nothing.
    func isDirty(comparedTo live: APIConfiguration) -> Bool {
        guard let pending = buildConfiguration() else { return false }
        return pending != live
    }

    /// Probes whatever address is currently in the field, falling back to the live one only when the field
    /// is unusable.
    func testConnection(fallback live: APIConfiguration) async {
        let target = buildConfiguration() ?? live
        let label = Self.displayHost(target.baseURL)
        connectionTest = .testing
        var request = URLRequest(url: target.healthzURL)
        request.timeoutInterval = 8
        request.cachePolicy = .reloadIgnoringLocalCacheData
        do {
            let (_, response) = try await URLSession.shared.data(for: request)
            if let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) {
                connectionTest = .ok(label)
            } else {
                let code = (response as? HTTPURLResponse)?.statusCode ?? 0
                connectionTest = .unreachable(label, "Answered with \(code) — that host isn't Sali.")
            }
        } catch {
            connectionTest = .unreachable(label, "Nothing answered at that address.")
        }
    }

    static func displayHost(_ url: URL) -> String {
        guard let host = url.host else { return url.absoluteString }
        return url.port.map { "\(host):\($0)" } ?? host
    }
}

struct SettingsView: View {
    @EnvironmentObject var appState: AppState
    @EnvironmentObject var auth: AuthService
    @EnvironmentObject var push: PushRegistration
    @StateObject private var viewModel = SettingsViewModel()
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    @State private var showSaveConfirmation = false
    @State private var showSignOutConfirmation = false
    @State private var modelError: String?
    @State private var showEraseConfirmation = false
    @State private var sudoDraft: String = ""
    @State private var sudoConfigured: Bool?
    @State private var sudoSaving = false
    @State private var sudoError: String?
    @State private var eraseConfirmText = ""
    @State private var isErasing = false
    @State private var eraseResult: String?

    @FocusState private var baseURLFocused: Bool

    @ScaledMetric(relativeTo: .caption2) private var inlineMarkSize: CGFloat = 13

    // Pushed as a NavigationLink destination from the Sali hub, so — like NotificationsView — this does not
    // open its own NavigationStack; it shares the one already on screen (which is also what lets
    // `securitySection`'s NavigationLink to DevicesView push correctly).
    var body: some View {
        List {
            connectionSection
            deviceSection
            modelSection
            securitySection
            notificationsSection
            aboutSection
            protectionSection
            startOverSection
        }
        .listStyle(.insetGrouped)
        .saliList()
        .navigationTitle("Settings")
        .task { viewModel.configureIfNeeded(from: appState.configuration) }
        .onChange(of: appState.configuration) { _, newValue in
            // Somebody else changed where the app points (Onboarding's Advanced panel). Adopt it unless
            // there is an unapplied edit in progress, which would be rude to discard.
            guard !viewModel.isDirty(comparedTo: newValue) else { return }
            viewModel.reseed(from: newValue)
        }
        .sheet(isPresented: $showSaveConfirmation) {
            if let pending = viewModel.buildConfiguration() {
                NaturalConfirmationSheet(
                    title: "Reconnect Sali",
                    explanation: "This points the app at \(pending.baseURL.absoluteString) (\(pending.environment.title)) and rebuilds the live session against it. Your device stays paired — this only changes where the app looks for Sali, never who it is allowed to be.",
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
                explanation: "This removes the saved password and tokens from this iPhone only, and returns you to the sign-in screen. To cut off access everywhere — including a lost phone — change the password on the machine with `sali change-password`, which logs every device out immediately.",
                confirmLabel: "Sign out",
                isDestructive: true
            ) {
                appState.signOut()
            }
        }
        .alert("Couldn't switch model", isPresented: Binding(
            get: { modelError != nil }, set: { if !$0 { modelError = nil } })) {
            Button("OK", role: .cancel) { modelError = nil }
        } message: {
            Text(modelError ?? "")
        }
        // Typing the word is the point: this is the one control here that cannot be undone, so it must
        // not be reachable by a mis-tap. The server demands the same phrase, so a typo fails safely.
        .alert("Erase everything?", isPresented: $showEraseConfirmation) {
            TextField("Type ERASE", text: $eraseConfirmText)
                .textInputAutocapitalization(.characters)
                .autocorrectionDisabled()
            Button("Cancel", role: .cancel) { eraseConfirmText = "" }
            Button("Erase everything", role: .destructive) {
                let phrase = eraseConfirmText
                eraseConfirmText = ""
                Task { await eraseEverything(phrase: phrase) }
            }
        } message: {
            Text("Every memory, chat, task and log is deleted and Sali starts fresh. Your password and this iPhone stay signed in, and files already saved on the machine aren't touched.\n\nThis cannot be undone — type ERASE to confirm.")
        }
        .alert("Start over", isPresented: Binding(
            get: { eraseResult != nil }, set: { if !$0 { eraseResult = nil } })) {
            Button("OK", role: .cancel) { eraseResult = nil }
        } message: {
            Text(eraseResult ?? "")
        }
    }

    // MARK: - Start over (destructive)

    /// Erase everything Sali knows so a test install can become a production one. Deliberately the LAST
    /// thing on the screen and the only destructive control here.
    private var startOverSection: some View {
        Section {
            Button(role: .destructive) {
                eraseConfirmText = ""
                showEraseConfirmation = true
            } label: {
                HStack(spacing: Theme.Spacing.m) {
                    Image(systemName: "trash")
                        .font(Theme.Typography.footnote)
                        .frame(width: 20)
                        .accessibilityHidden(true)
                    Text(isErasing ? "Erasing…" : "Erase everything")
                    Spacer()
                    if isErasing { ProgressView() }
                }
                .frame(minHeight: 32)
            }
            .disabled(isErasing)
        } header: {
            SectionHeader("Start over", subtitle: "Give Sali a clean slate for production.")
        } footer: {
            SectionFooter("Deletes every memory, chat, task and the whole activity log — everything Sali has learned or done. Your password, this iPhone's sign-in and the model you picked are kept, so the app keeps working. Files already saved on the machine are left alone.")
        }
    }

    /// POST the reset, then drop everything this app is still holding. The local wipe is not cosmetic:
    /// the transcript, the event buffer and the replay watermark all point at rows that no longer exist.
    private func eraseEverything(phrase: String) async {
        isErasing = true
        defer { isErasing = false }
        do {
            try await appState.api.postVoid("factory-reset", json: ["confirm": phrase])
            appState.resetLocalStateAfterErase()
            Haptics.success()
            eraseResult = "Sali is back to the beginning. Memories, chats, tasks and logs are gone — and you're still signed in."
        } catch {
            Haptics.failure()
            eraseResult = (error as? LocalizedError)?.errorDescription
                ?? "Couldn't erase — check the connection and try again."
        }
    }

    // MARK: - Model switcher

    /// Pick Sali's active model. `vision` on a row governs whether the chat image button shows once
    /// that model is active; a model without `tools` can't be selected (Sali needs tools to act).
    private var modelSection: some View {
        Section {
            if appState.availableModels.isEmpty {
                Text("No models found on the host.")
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .frame(minHeight: 44)
            } else {
                ForEach(appState.availableModels) { m in
                    Button {
                        guard !m.active, !appState.switchingModel, m.tools else { return }
                        Task {
                            do { try await appState.setModel(m.name) }
                            catch { modelError = Self.modelErrorText(error) }
                        }
                    } label: {
                        HStack(spacing: Theme.Spacing.s) {
                            VStack(alignment: .leading, spacing: 3) {
                                Text(m.name)
                                    .font(Theme.Typography.body)
                                    .foregroundStyle(Theme.Colors.primaryText)
                                    .lineLimit(1)
                                HStack(spacing: Theme.Spacing.xs) {
                                    if let p = m.parameterSize { modelBadge(p) }
                                    if let q = m.quantization { modelBadge(q) }
                                    modelBadge(m.vision ? "vision" : "no vision",
                                               muted: !m.vision)
                                    modelBadge(m.tools ? "tools" : "no tools",
                                               muted: !m.tools, warn: !m.tools)
                                }
                            }
                            Spacer(minLength: Theme.Spacing.s)
                            if m.active {
                                Image(systemName: "checkmark.circle.fill")
                                    .foregroundStyle(Theme.Colors.accent)
                            } else if appState.switchingModel {
                                ProgressView()
                            }
                        }
                        .frame(minHeight: 44)
                        .contentShape(Rectangle())
                        .opacity(m.tools ? 1 : 0.5)
                    }
                    .buttonStyle(.plain)
                    .disabled(!m.tools || appState.switchingModel)
                }
            }
        } header: {
            SectionHeader("Model", subtitle: "Sali's brain — switching reloads it (a few seconds).")
        } footer: {
            SectionFooter("A model with no vision hides the chat image button. Tools are required, so a model without them can't be picked. Switching is blocked while Sali is busy.")
        }
        .task { await appState.refreshModels() }
    }

    private func modelBadge(_ text: String, muted: Bool = false, warn: Bool = false) -> some View {
        Text(text)
            .font(Theme.Typography.metadata)
            .foregroundStyle(warn ? Theme.Colors.warn
                             : muted ? Theme.Colors.tertiaryText : Theme.Colors.secondaryText)
            .padding(.horizontal, Theme.Spacing.xs)
            .padding(.vertical, 2)
            .background(Theme.Colors.surfaceRaised, in: Capsule())
    }

    private static func modelErrorText(_ error: Error) -> String {
        let msg = (error as NSError).localizedDescription
        if msg.contains("409") || msg.lowercased().contains("busy") {
            return "Sali is busy right now — try switching when it's idle."
        }
        if msg.contains("422") || msg.lowercased().contains("tool") {
            return "That model doesn't support tool calling, so Sali can't use it."
        }
        if msg.contains("404") { return "That model isn't installed on the host." }
        return "Switch failed: \(msg)"
    }

    // MARK: - Connection

    private var connectionSection: some View {
        Section {
            liveAddressRow
            environmentRow
            baseURLRow
            testConnectionRow
            // Only when there is something to save. A permanently-disabled filled button is the visually
            // loudest thing on the screen while being the one thing that can't be tapped; the footer's
            // "Unsaved" marker and this row appearing together are the honest signal that an edit is
            // pending.
            if viewModel.isDirty(comparedTo: appState.configuration) {
                saveRow
            }
        } header: {
            SectionHeader("Connection", subtitle: "Where this iPhone looks for Sali.")
        } footer: {
            SectionFooter(
                "Development points at your machine on the LAN; Production goes through the Cloudflare Tunnel. Changing this never bypasses Sali's own authentication — a wrong address simply can't answer.",
                detail: viewModel.isDirty(comparedTo: appState.configuration) ? "Unsaved" : nil
            )
        }
    }

    /// What the app is talking to *right now* — a fact, not a field. Separating it from the editable text
    /// is the only way an in-progress edit can't be mistaken for the live setting.
    private var liveAddressRow: some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.m) {
            VStack(alignment: .leading, spacing: 2) {
                Text("Connected to")
                    .font(Theme.Typography.body)
                    .foregroundStyle(Theme.Colors.primaryText)
                Text(SettingsViewModel.displayHost(appState.configuration.baseURL))
                    .font(Theme.Typography.monoSmall)
                    .foregroundStyle(Theme.Colors.tertiaryText)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            Spacer(minLength: Theme.Spacing.s)
            ConnectionBadge(appState.ws.state)
        }
        .frame(minHeight: 44)
        .accessibilityElement(children: .combine)
    }

    private var environmentRow: some View {
        Picker("Environment", selection: $viewModel.selectedEnvironment) {
            ForEach(APIEnvironment.allCases, id: \.self) { env in
                Text(env.title).tag(env)
            }
        }
        .frame(minHeight: 44)
    }

    private var baseURLRow: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
            Text("Base URL")
                .font(Theme.Typography.subheading)
                .foregroundStyle(Theme.Colors.secondaryText)

            TextField("https://sali.example.com", text: $viewModel.baseURLText)
                .keyboardType(.URL)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .font(Theme.Typography.mono)
                .foregroundStyle(Theme.Colors.primaryText)
                .focused($baseURLFocused)
                .modifier(ConnectionFieldWell(isFocused: baseURLFocused, reduceMotion: reduceMotion))
                .accessibilityLabel("Server base URL")

            if viewModel.addressLooksWrong {
                Label {
                    Text("Needs a scheme and a host — like https://sali.example.com or http://192.168.1.20:8080.")
                        .font(Theme.Typography.footnote)
                } icon: {
                    Image(systemName: "exclamationmark.circle").font(Theme.Typography.caption)
                }
                .foregroundStyle(Theme.Colors.danger)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityElement(children: .combine)
            }
        }
        .padding(.vertical, Theme.Spacing.xs)
        .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.quick), value: viewModel.addressLooksWrong)
    }

    private var testConnectionRow: some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.m) {
            // A bordered control, not tinted text. `accent` IS ink, so a plain text button in a list row
            // is pixel-identical to a static label — there was nothing to tell the person this was tappable.
            Button("Test connection") {
                Task { await viewModel.testConnection(fallback: appState.configuration) }
            }
            .font(Theme.Typography.footnote.weight(.semibold))
            .buttonStyle(.bordered)
            .tint(Theme.Colors.accent)
            .frame(minHeight: 44, alignment: .leading)
            .contentShape(Rectangle())
            .disabled(viewModel.connectionTest == .testing)

            Spacer(minLength: Theme.Spacing.s)
            testResultView
        }
        .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.gentle), value: viewModel.connectionTest)
    }

    /// Every outcome names the host it describes — a result with no address attached is how a stale
    /// "Reachable" ends up sitting beside a brand-new URL.
    @ViewBuilder private var testResultView: some View {
        switch viewModel.connectionTest {
        case .idle:
            Text("Not checked")
                .font(Theme.Typography.metadata)
                .foregroundStyle(Theme.Colors.tertiaryText)
        case .testing:
            HStack(spacing: Theme.Spacing.xs) {
                SaliMark(size: inlineMarkSize, color: Theme.Colors.secondaryText, animated: !reduceMotion)
                Text("Checking…")
                    .font(Theme.Typography.metadata)
                    .foregroundStyle(Theme.Colors.tertiaryText)
            }
        case .ok(let host):
            resultLabel("Reachable", detail: host, icon: "checkmark.circle",
                        tint: Theme.Colors.accent)
        case .unreachable(let host, let reason):
            resultLabel(reason, detail: host, icon: "wifi.exclamationmark",
                        tint: Theme.Colors.danger)
        }
    }

    private func resultLabel(_ text: String, detail: String, icon: String, tint: Color) -> some View {
        VStack(alignment: .trailing, spacing: 2) {
            Label {
                Text(text).font(Theme.Typography.caption)
            } icon: {
                Image(systemName: icon).font(Theme.Typography.caption)
            }
            .foregroundStyle(tint)
            Text(detail)
                .font(Theme.Typography.metadata)
                .foregroundStyle(Theme.Colors.tertiaryText)
                .lineLimit(1)
                .truncationMode(.middle)
        }
        .multilineTextAlignment(.trailing)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(text) at \(detail)")
    }

    private var saveRow: some View {
        Button {
            showSaveConfirmation = true
        } label: {
            Text("Save & reconnect").frame(maxWidth: .infinity)
        }
        .buttonStyle(SaliPrimaryButtonStyle())
        // Stays INSIDE the section. A `.listRowBackground(.clear)` row at the end of an inset-grouped
        // section is still part of that section's background shape, so the card's bottom corners square
        // off — the button floated on the canvas under a card that had visibly lost its rounding.
        .padding(.vertical, Theme.Spacing.xs)
        .accessibilityHint("Points the app at the address above and reconnects.")
    }

    // MARK: - This device

    private var deviceSection: some View {
        Section {
            valueRow("Name", value: auth.deviceName.isEmpty ? "This iPhone" : auth.deviceName)

            HStack {
                Text("Role")
                    .font(Theme.Typography.body)
                    .foregroundStyle(Theme.Colors.primaryText)
                Spacer(minLength: Theme.Spacing.s)
                StatusPill(appState.role.label, color: roleInk)
            }
            .frame(minHeight: 44)
            .accessibilityElement(children: .combine)

            Text(roleExplanation)
                .font(Theme.Typography.footnote)
                .foregroundStyle(Theme.Colors.secondaryText)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.vertical, Theme.Spacing.xs)

            Button(role: .destructive) {
                showSignOutConfirmation = true
            } label: {
                Text("Sign out (this device)")
                    .font(Theme.Typography.body)
                    .foregroundStyle(Theme.Colors.danger)
                    .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
        } header: {
            SectionHeader("This device")
        } footer: {
            SectionFooter("Signing out only clears this iPhone's saved password and tokens. To cut off access remotely — including a lost or stolen phone — change the password on the machine with `sali change-password`; it revokes every session immediately, even while the phone is offline.")
        }
    }

    /// The role this device holds is a fact about it, not a warning — ink for authority, quiet ink for
    /// read-only (item 12).
    private var roleInk: Color {
        switch appState.role {
        case .owner: Theme.Colors.accent
        case .controller: Theme.Colors.secondaryText
        case .observer: Theme.Colors.idle
        }
    }

    private var roleExplanation: String {
        switch appState.role {
        case .owner: "Full authority: this iPhone can direct Sali and pair or revoke other devices."
        case .controller: "This iPhone can direct Sali, but can't pair or revoke devices."
        case .observer: "Read-only. This iPhone can watch what Sali is doing without directing it."
        }
    }

    // MARK: - Security

    private var securitySection: some View {
        Section {
            NavigationLink {
                DevicesView()
            } label: {
                HStack(spacing: Theme.Spacing.m) {
                    Image(systemName: "iphone.and.arrow.forward")
                        .font(Theme.Typography.body)
                        .foregroundStyle(Theme.Colors.secondaryText)
                        .frame(width: 24)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Devices")
                            .font(Theme.Typography.body)
                            .foregroundStyle(Theme.Colors.primaryText)
                        Text(appState.role.canManageDevices
                             ? "Pair a new device, or revoke one."
                             : "Owner only — you can see this list is managed elsewhere.")
                            .font(Theme.Typography.footnote)
                            .foregroundStyle(Theme.Colors.secondaryText)
                    }
                }
                .frame(minHeight: 44)
            }
            sudoRow
        } header: {
            SectionHeader("Security")
        } footer: {
            Text("The sudo password is stored encrypted on your machine and handed straight to sudo. "
                 + "Sali never sees it — not in his prompt, his logs, or his memory.")
                .font(Theme.Typography.footnote)
                .foregroundStyle(Theme.Colors.secondaryText)
        }
    }

    /// Hand Sali the sudo password without ever putting it in front of him.
    ///
    /// He can ask for it out loud ("I need sudo for that — set it in Settings"); what he must never do
    /// is RECEIVE it, because anything he receives is in the model's context, the turn journal and
    /// potentially his memory. This field posts it to the vault instead, and the only thing that ever
    /// reads it back is the askpass helper piping it into sudo. There is no way to read it out here —
    /// the API exposes whether one is set, never the value.
    @ViewBuilder
    private var sudoRow: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            HStack(spacing: Theme.Spacing.m) {
                Image(systemName: "lock.shield")
                    .font(Theme.Typography.body)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .frame(width: 24)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Sudo password")
                        .font(Theme.Typography.body)
                        .foregroundStyle(Theme.Colors.primaryText)
                    Text(sudoConfigured == true
                         ? "Set — Sali can do elevated work on his machine."
                         : sudoConfigured == false
                           ? "Not set — he'll have to ask you for anything needing root."
                           : "Checking…")
                        .font(Theme.Typography.footnote)
                        .foregroundStyle(Theme.Colors.secondaryText)
                }
            }
            SecureField(sudoConfigured == true ? "Replace it" : "Set it", text: $sudoDraft)
                .textContentType(.password)
                .autocorrectionDisabled()
                .textInputAutocapitalization(.never)
                .frame(minHeight: 44)
            if let sudoError {
                Text(sudoError)
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.danger)
            }
            Button(sudoSaving ? "Saving…" : "Save to the vault") { Task { await saveSudoPassword() } }
                .disabled(sudoDraft.trimmingCharacters(in: .whitespaces).isEmpty || sudoSaving)
                .frame(minHeight: 44)
        }
        .padding(.vertical, Theme.Spacing.xs)
        .task { await loadSudoStatus() }
    }

    private func loadSudoStatus() async {
        struct Status: Decodable { let configured: Bool }
        guard let s: Status = try? await appState.api.get("secrets/sudo") else { return }
        sudoConfigured = s.configured
    }

    private func saveSudoPassword() async {
        sudoSaving = true
        sudoError = nil
        defer { sudoSaving = false }
        struct Status: Decodable { let configured: Bool }
        do {
            let s: Status = try await appState.api.post(
                "secrets/sudo", json: ["password": sudoDraft])
            sudoConfigured = s.configured
            sudoDraft = ""          // never keep it in memory a moment longer than the request
            Haptics.success()
        } catch {
            sudoError = (error as? LocalizedError)?.errorDescription ?? "Couldn't save that."
        }
    }

    // MARK: - Notifications
    //
    // Still no switch of our own: the switch that decides this is the one in iOS Settings, and a second
    // one here would be a copy that can silently disagree with it. What this section has instead is a
    // button that raises the system prompt once, and an honest read of what iOS answered.

    private var notificationsSection: some View {
        Section {
            allowNotificationsRow
            statusRow(title: "Messages from Sali",
                      detail: "Kept in Inbox, and they survive a relaunch.",
                      state: "On")
        } header: {
            SectionHeader("Notifications")
        } footer: {
            SectionFooter("Permission and token registration are real: allowing this hands Sali's server this iPhone's APNs token. Sali has nowhere to push from yet — the server stores the token but has no sender — so no banner will arrive from Sali until that ships. Its messages still reach you over the live connection and are kept in Inbox.")
        }
    }

    /// The permission row. Reads its state from iOS every time the app becomes active, so it cannot drift
    /// from the actual system setting, and offers exactly the one action that state allows: the prompt
    /// while it can still be raised, and afterwards a trip to iOS Settings, which is the only place a
    /// refusal can be undone.
    private var allowNotificationsRow: some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.m) {
            VStack(alignment: .leading, spacing: 2) {
                Text("Banners on this iPhone")
                    .font(Theme.Typography.body)
                    .foregroundStyle(Theme.Colors.primaryText)
                Text(pushDetail)
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .fixedSize(horizontal: false, vertical: true)

                switch push.state {
                case .notAsked:
                    pushButton("Allow notifications") { Task { await push.requestPermission() } }
                case .denied:
                    pushButton("Open iOS Settings") { push.openSystemSettings() }
                default:
                    EmptyView()
                }
            }
            Spacer(minLength: Theme.Spacing.s)
            StatusPill(pushState)
        }
        .frame(minHeight: 44)
        .padding(.vertical, Theme.Spacing.xs)
        .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.gentle), value: pushState)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Banners on this iPhone: \(pushState). \(pushDetail)")
    }

    private func pushButton(_ title: String, action: @escaping () -> Void) -> some View {
        Button(title, action: action)
            .font(Theme.Typography.footnote.weight(.semibold))
            .buttonStyle(.bordered)
            .tint(Theme.Colors.accent)
            .frame(minHeight: 44, alignment: .leading)
            .contentShape(Rectangle())
            .disabled(push.isWorking)
            .padding(.top, Theme.Spacing.xs)
    }

    private var pushState: String {
        switch push.state {
        case .notAsked:            "Not asked"
        case .denied:              "Off"
        case .allowedWithoutToken: "No token"
        case .tokenPending:        "Not sent"
        case .registered:          "Registered"
        case .failed:              "Failed"
        }
    }

    private var pushDetail: String {
        switch push.state {
        case .notAsked:
            "iOS hasn't been asked yet. Allowing it registers this iPhone with Sali for later."
        case .denied:
            "Turned off in iOS Settings — only iOS can turn it back on."
        case .allowedWithoutToken:
            "Allowed, but APNs hasn't issued a token for this build yet."
        case .tokenPending:
            "iOS issued a token; Sali hasn't taken it yet."
        case .registered:
            "Allowed, and Sali's server holds this iPhone's token (\(PushRegistration.apsEnvironment))."
        case .failed(let reason):
            reason
        }
    }

    private func statusRow(title: String, detail: String, state: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.m) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(Theme.Typography.body)
                    .foregroundStyle(Theme.Colors.primaryText)
                Text(detail)
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: Theme.Spacing.s)
            StatusPill(state)
        }
        .frame(minHeight: 44)
        .padding(.vertical, Theme.Spacing.xs)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(title): \(state). \(detail)")
    }

    // MARK: - About

    private var aboutSection: some View {
        Section {
            valueRow("Version", value: appVersion, monospaced: true)
        } header: {
            SectionHeader("About")
        } footer: {
            SectionFooter("One Sali, one conversation. This app is a window into the same Sali instance you talk to on the terminal — never a second assistant.")
        }
    }

    private var protectionSection: some View {
        Section {
            protectionRow("Credentials live only in the iOS Keychain", icon: "key")
            protectionRow("Cloudflare Tunnel plus Sali's own auth — defence in depth", icon: "lock.shield")
            protectionRow("Secrets are never shown in plaintext or logged", icon: "eye.slash")
        } header: {
            SectionHeader("How this iPhone is protected", emphasis: .secondary)
        }
    }

    private func protectionRow(_ text: String, icon: String) -> some View {
        HStack(alignment: .top, spacing: Theme.Spacing.m) {
            Image(systemName: icon)
                .font(Theme.Typography.footnote)
                .foregroundStyle(Theme.Colors.tertiaryText)
                .frame(width: 20)
                .accessibilityHidden(true)
            Text(text)
                .font(Theme.Typography.footnote)
                .foregroundStyle(Theme.Colors.secondaryText)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(minHeight: 32)
        .padding(.vertical, Theme.Spacing.xs)
        .accessibilityElement(children: .combine)
    }

    // MARK: - Shared row shapes

    private func valueRow(_ label: String, value: String, monospaced: Bool = false) -> some View {
        HStack(spacing: Theme.Spacing.m) {
            Text(label)
                .font(Theme.Typography.body)
                .foregroundStyle(Theme.Colors.primaryText)
            Spacer(minLength: Theme.Spacing.s)
            Text(value)
                .font(monospaced ? Theme.Typography.monoSmall : Theme.Typography.footnote)
                .foregroundStyle(Theme.Colors.secondaryText)
                .lineLimit(1)
                .truncationMode(.middle)
        }
        .frame(minHeight: 44)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(label): \(value)")
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

// MARK: - The input well

/// Text fields belong on `surfaceSunken` — recessed a step below the row they sit in, edged with the one
/// hairline, thickening to `Stroke.emphasis` on focus. Without it a `TextField` in a grouped list row is an
/// invisible affordance: same colour as the row, no edge, nothing that says "type here".
private struct ConnectionFieldWell: ViewModifier {
    let isFocused: Bool
    let reduceMotion: Bool

    func body(content: Content) -> some View {
        content
            .padding(.horizontal, Theme.Spacing.m)
            .padding(.vertical, Theme.Spacing.s)
            .frame(minHeight: 44)
            .background(Theme.Colors.surfaceSunken)
            .clipShape(RoundedRectangle(cornerRadius: Theme.Radius.s, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: Theme.Radius.s, style: .continuous)
                    .strokeBorder(isFocused ? Theme.Colors.borderStrong : Theme.Colors.border,
                                  lineWidth: isFocused ? Theme.Stroke.emphasis : Theme.Stroke.hairline)
            )
            .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.quick), value: isFocused)
    }
}
