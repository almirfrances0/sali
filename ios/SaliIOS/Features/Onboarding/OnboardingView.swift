import SwiftUI
#if canImport(UIKit)
import UIKit
#endif

/// One-time device pairing (§23). A fresh install has no authority over Sali until this screen succeeds:
/// `RootView` shows nothing else while `auth.isEnrolled == false`. The flow is deliberately narrow — one
/// code, one name, one button — because pairing is a single decisive act, not a settings page. On success
/// `AppState.didEnroll()` opens the live connection and `RootView` swaps to the tabbed control center.
///
/// An "Advanced" disclosure (§32) lets a developer point at a LAN IP (or staging) without recompiling —
/// it reuses `AppState.applyConfiguration`, the same call Settings will make later.
///
/// `Package.swift` declares both `.iOS(.v17)` and `.macOS(.v14)` (so the non-UI layers can be unit-tested
/// with `swift test` on the Mac). `textInputAutocapitalization`, `keyboardType`, `submitLabel`, and
/// `scrollDismissesKeyboard` have no plain-macOS overload, so their call sites below are wrapped in
/// `#if os(iOS)` — otherwise this file would fail to compile the moment the package is built for macOS.
struct OnboardingView: View {
    @EnvironmentObject private var appState: AppState
    @EnvironmentObject private var auth: AuthService
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    @State private var code = ""
    @State private var deviceName = OnboardingView.defaultDeviceName()
    @State private var isEnrolling = false
    @State private var errorMessage: String?

    // Advanced / server address (§32) — seeded from the persisted configuration in `onAppear`, since
    // `@EnvironmentObject` isn't available yet at `init`.
    @State private var environment: APIEnvironment = .production
    @State private var baseURLText: String = APIEnvironment.production.defaultBaseURL.absoluteString

    @FocusState private var focusedField: Field?
    private enum Field: Hashable { case code, name, baseURL }

    var body: some View {
        ScrollView {
            VStack(spacing: Theme.Spacing.xxl) {
                header
                formCard
                advancedDisclosure
            }
            .padding(Theme.Spacing.l)
            .frame(maxWidth: 480)
            .frame(maxWidth: .infinity)
        }
        .background(Theme.Colors.background.ignoresSafeArea())
        #if os(iOS)
        .scrollDismissesKeyboard(.interactively)
        #endif
        .onAppear {
            environment = appState.configuration.environment
            baseURLText = appState.configuration.baseURL.absoluteString
        }
    }

    // MARK: - Sections

    private var header: some View {
        VStack(spacing: Theme.Spacing.m) {
            ZStack {
                Circle().fill(Theme.Colors.accentSoft).frame(width: 96, height: 96)
                Image(systemName: "sparkles")
                    .font(.system(size: 36, weight: .medium))
                    .foregroundStyle(Theme.Colors.accent)
                    .symbolEffect(.pulse, isActive: !reduceMotion)
            }
            .accessibilityHidden(true)
            .padding(.top, Theme.Spacing.xxl)

            Text("Meet Sali")
                .font(Theme.Typography.title)
                .foregroundStyle(Theme.Colors.primaryText)

            Text("""
            Sali runs on your Kali machine at home. Pair this iPhone once, and it becomes a trusted \
            window into what Sali is doing and asking of you.
            """)
                .font(Theme.Typography.body)
                .foregroundStyle(Theme.Colors.secondaryText)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var formCard: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.l) {
            SectionHeader("Pairing code")

            VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                TextField("XXXXX-XXXXX", text: $code)
                    #if os(iOS)
                    .textInputAutocapitalization(.characters)
                    .keyboardType(.asciiCapable)
                    #endif
                    .autocorrectionDisabled()
                    .font(Theme.Typography.mono)
                    .focused($focusedField, equals: .code)
                    #if os(iOS)
                    .submitLabel(.next)
                    #endif
                    .onSubmit { focusedField = .name }
                    .onChange(of: code) { _, newValue in code = Self.formatCode(newValue) }
                    .padding(Theme.Spacing.m)
                    .background(Theme.Colors.surfaceRaised)
                    .clipShape(RoundedRectangle(cornerRadius: Theme.Radius.s, style: .continuous))
                    .accessibilityLabel("Pairing code")
                    .accessibilityHint("Five letters or numbers, a dash, then five more, from sali enroll-code.")

                Text("On the Sali machine, run `sali enroll-code` and type the code here.")
                    .font(Theme.Typography.caption)
                    .foregroundStyle(Theme.Colors.secondaryText)
            }

            VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                Text("Device name")
                    .font(Theme.Typography.caption)
                    .foregroundStyle(Theme.Colors.secondaryText)
                TextField("This iPhone", text: $deviceName)
                    #if os(iOS)
                    .textInputAutocapitalization(.words)
                    #endif
                    .autocorrectionDisabled()
                    .focused($focusedField, equals: .name)
                    #if os(iOS)
                    .submitLabel(.go)
                    #endif
                    .onSubmit { Task { await submit() } }
                    .padding(Theme.Spacing.m)
                    .background(Theme.Colors.surfaceRaised)
                    .clipShape(RoundedRectangle(cornerRadius: Theme.Radius.s, style: .continuous))
                    .accessibilityLabel("Device name")
            }

            if let errorMessage {
                Label {
                    Text(errorMessage).font(Theme.Typography.caption)
                } icon: {
                    Image(systemName: "exclamationmark.triangle.fill")
                }
                .foregroundStyle(Theme.Colors.danger)
                .accessibilityElement(children: .combine)
                .accessibilityLabel("Error: \(errorMessage)")
            }

            Button {
                Task { await submit() }
            } label: {
                HStack(spacing: Theme.Spacing.s) {
                    if isEnrolling {
                        ProgressView().tint(.white)
                    }
                    Text(isEnrolling ? "Pairing…" : "Pair this iPhone")
                        .font(Theme.Typography.heading)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, Theme.Spacing.s)
            }
            .buttonStyle(.borderedProminent)
            .tint(Theme.Colors.accent)
            .disabled(!isSubmittable || isEnrolling)
            .accessibilityLabel(isEnrolling ? "Pairing this iPhone" : "Pair this iPhone")
        }
        .saliCard()
    }

    private var advancedDisclosure: some View {
        DisclosureGroup("Advanced: server address") {
            VStack(alignment: .leading, spacing: Theme.Spacing.m) {
                Picker("Environment", selection: $environment) {
                    ForEach(APIEnvironment.allCases, id: \.self) { env in
                        Text(env.title).tag(env)
                    }
                }
                .pickerStyle(.segmented)
                .onChange(of: environment) { _, newValue in
                    baseURLText = newValue.defaultBaseURL.absoluteString
                }

                VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                    Text("Base URL")
                        .font(Theme.Typography.caption)
                        .foregroundStyle(Theme.Colors.secondaryText)
                    TextField("http://192.168.1.20:8080", text: $baseURLText)
                        #if os(iOS)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        #endif
                        .autocorrectionDisabled()
                        .focused($focusedField, equals: .baseURL)
                        .padding(Theme.Spacing.m)
                        .background(Theme.Colors.surfaceRaised)
                        .clipShape(RoundedRectangle(cornerRadius: Theme.Radius.s, style: .continuous))
                        .accessibilityLabel("Server base URL")
                }

                Text("Point at your Kali box's LAN address for development — no recompile needed.")
                    .font(Theme.Typography.caption)
                    .foregroundStyle(Theme.Colors.secondaryText)

                Button("Apply server address") { applyServerAddress() }
                    .buttonStyle(.bordered)
                    .tint(Theme.Colors.accent)
                    .disabled(URL(string: baseURLText) == nil)
            }
            .padding(.top, Theme.Spacing.s)
        }
        .font(Theme.Typography.heading)
        .padding(Theme.Spacing.l)
        .background(Theme.Colors.surface)
        .clipShape(RoundedRectangle(cornerRadius: Theme.Radius.m, style: .continuous))
        .tint(Theme.Colors.accent)
        .accessibilityLabel("Advanced, server address")
    }

    // MARK: - Actions

    @MainActor
    private func submit() async {
        guard isSubmittable, !isEnrolling else { return }
        focusedField = nil
        errorMessage = nil
        isEnrolling = true
        defer { isEnrolling = false }
        do {
            try await auth.enroll(
                code: code,
                deviceName: deviceName.trimmingCharacters(in: .whitespacesAndNewlines),
                model: Self.modelIdentifier()
            )
            appState.didEnroll()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func applyServerAddress() {
        guard let url = URL(string: baseURLText) else { return }
        appState.applyConfiguration(APIConfiguration(environment: environment, baseURL: url))
    }

    // MARK: - Validation & formatting

    private var isSubmittable: Bool {
        Self.isValidCode(code) && !deviceName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    /// Reformats free-typed input into `XXXXX-XXXXX` as the person types — tolerant of lowercase, stray
    /// dashes, and spaces (people will paste the code however the terminal wrapped it).
    private static func formatCode(_ raw: String) -> String {
        let alnum = raw.uppercased().filter { $0.isLetter || $0.isNumber }
        let limited = String(alnum.prefix(10))
        guard limited.count > 5 else { return limited }
        return "\(limited.prefix(5))-\(limited.dropFirst(5))"
    }

    private static func isValidCode(_ value: String) -> Bool {
        let parts = value.split(separator: "-", omittingEmptySubsequences: false)
        guard parts.count == 2 else { return false }
        return parts[0].count == 5 && parts[1].count == 5
    }

    // MARK: - Device identity

    private static func defaultDeviceName() -> String {
        #if canImport(UIKit)
        return UIDevice.current.name
        #else
        return "This device"
        #endif
    }

    /// The hardware model identifier (e.g. "iPhone16,1"), read via `uname`. Falls back to the coarser
    /// `UIDevice.current.model` (e.g. "iPhone") if the sysctl lookup ever comes back empty — never
    /// fabricated (golden rule: never guess when the system can inspect the truth).
    private static func modelIdentifier() -> String {
        #if canImport(UIKit)
        var systemInfo = utsname()
        uname(&systemInfo)
        let raw = withUnsafePointer(to: &systemInfo.machine) { ptr -> String in
            ptr.withMemoryRebound(to: CChar.self, capacity: 1) { String(cString: $0) }
        }
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? UIDevice.current.model : trimmed
        #else
        return "unknown"
        #endif
    }
}

#Preview {
    let appState = AppState()
    return OnboardingView()
        .environmentObject(appState)
        .environmentObject(appState.auth)
}
