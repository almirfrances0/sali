import SwiftUI
#if canImport(UIKit)
import UIKit
#endif

/// The first screen: connect this iPhone to YOUR Sali. Password auth (not a device pairing code), so the
/// setup is two things anyone can share and reproduce — the server address of a Sali running on some
/// machine, and the password set there with `sali change-password`.
///
/// The address is NOT hardcoded: you type where your Sali lives (a LAN IP, a tunnel host, anything), which
/// is what makes the project shareable. If Sali is on the same Wi-Fi it is auto-detected and the field is
/// filled for you. The password is the durable credential — once you're in, an expired token silently
/// re-authenticates with it, so you're never bounced back here while you're away from the machine.
struct OnboardingView: View {
    @EnvironmentObject private var appState: AppState
    @EnvironmentObject private var auth: AuthService
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    @State private var serverURL = ""
    @State private var password = ""
    @State private var deviceName = OnboardingView.defaultDeviceName()
    @State private var isConnecting = false
    @State private var failure: String?

    @FocusState private var focusedField: Field?
    private enum Field: Hashable { case serverURL, password, name }

    @ScaledMetric(relativeTo: .largeTitle) private var markSize: CGFloat = 44
    @ScaledMetric(relativeTo: .largeTitle) private var markWell: CGFloat = 96

    var body: some View {
        ScrollView {
            VStack(spacing: Theme.Spacing.sectionGap) {
                header
                formCard
                assurance
            }
            .padding(.horizontal, Theme.Spacing.screenMargin)
            .padding(.bottom, Theme.Spacing.chapterGap)
            .frame(maxWidth: 480)
            .frame(maxWidth: .infinity)
        }
        .background(Theme.Colors.background.ignoresSafeArea())
        .scrollDismissesKeyboard(.interactively)
        .onAppear {
            if serverURL.isEmpty {
                serverURL = appState.tokenStore.hasConfiguration
                    ? appState.configuration.baseURL.absoluteString : ""
            }
            appState.connection.discovery.start()
        }
        .onDisappear { if !auth.isLoggedIn { appState.connection.discovery.stop() } }
        .onReceive(appState.connection.discovery.$services) { services in
            // Convenience only: if Sali is found on the LAN and the field is still empty, fill it in.
            guard !auth.isLoggedIn, serverURL.isEmpty || isLANFill else { return }
            if let candidate = services.values.first, let url = candidate.baseURL {
                serverURL = url.absoluteString
                isLANFill = true
            }
        }
    }

    // Tracks whether the current field value came from LAN auto-fill (so a later discovery update may
    // replace it), vs typed by the person (which auto-fill must never overwrite).
    @State private var isLANFill = false

    // MARK: - Header

    private var header: some View {
        VStack(spacing: Theme.Spacing.m) {
            ZStack {
                Circle().fill(Theme.Colors.accentSoft).frame(width: markWell, height: markWell)
                SaliMark(size: markSize, color: Theme.Colors.accent, animated: !reduceMotion)
            }
            .accessibilityHidden(true)
            .padding(.top, Theme.Spacing.chapterGap)

            Text("Meet Sali")
                .font(Theme.Typography.display)
                .foregroundStyle(Theme.Colors.primaryText)

            Text("""
            Sali runs on your own machine. Point this iPhone at where it lives and sign in with your \
            password — it becomes a trusted window into what Sali is doing, and what it needs from you.
            """)
                .font(Theme.Typography.callout)
                .foregroundStyle(Theme.Colors.secondaryText)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    // MARK: - Form

    private var formCard: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.l) {
            VStack(alignment: .leading, spacing: 2) {
                Text("Connect to your Sali")
                    .font(Theme.Typography.titleSmall)
                    .foregroundStyle(Theme.Colors.primaryText)
                Text("Set the password on the machine with `sali change-password`.")
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.secondaryText)
            }

            if let local = appState.connection.discovery.services.values.first {
                HStack(spacing: 6) {
                    Circle().fill(Theme.Colors.accent).frame(width: 7, height: 7)
                    Text("Found on your network (\(local.host):\(local.port))")
                        .font(Theme.Typography.caption)
                        .foregroundStyle(Theme.Colors.secondaryText)
                }
            }

            field(label: "Server address", systemImage: "network") {
                TextField("http://192.168.1.20:8080", text: $serverURL)
                    .textContentType(.URL)
                    .focused($focusedField, equals: .serverURL)
                    .submitLabel(.next)
                    .onChange(of: serverURL) { _, _ in isLANFill = false }
                    .onSubmit { focusedField = .password }
                    #if canImport(UIKit)
                    .keyboardType(.URL)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled(true)
                    #endif
                    .accessibilityLabel("Server address")
            }

            field(label: "Password", systemImage: "lock") {
                SecureField("Your Sali password", text: $password)
                    .textContentType(.password)
                    .focused($focusedField, equals: .password)
                    .submitLabel(.next)
                    .onSubmit { focusedField = .name }
                    .accessibilityLabel("Password")
            }

            field(label: "This iPhone", systemImage: "iphone") {
                TextField("This iPhone", text: $deviceName)
                    .textContentType(.name)
                    .focused($focusedField, equals: .name)
                    .submitLabel(.go)
                    .onSubmit { Task { await submit() } }
                    .accessibilityLabel("Device name")
            }

            if let failure {
                Text(failure)
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.danger)
                    .fixedSize(horizontal: false, vertical: true)
                    .transition(.opacity)
            }

            Button {
                Task { await submit() }
            } label: {
                HStack(spacing: Theme.Spacing.s) {
                    if isConnecting {
                        ProgressView().tint(Theme.Colors.onAccent)
                    }
                    Text(isConnecting ? "Connecting…" : "Connect")
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(SaliPrimaryButtonStyle())
            .disabled(!isSubmittable || isConnecting)
        }
        .padding(Theme.Spacing.l)
        .saliCard()
        .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.standard), value: failure)
        .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.quick), value: isConnecting)
    }

    /// One labeled field row — a small uppercase label over a bordered well, matching the app's field look.
    @ViewBuilder
    private func field<Content: View>(label: String, systemImage: String,
                                      @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: systemImage).font(Theme.Typography.caption)
                Text(label.uppercased()).font(Theme.Typography.metadata).tracking(0.6)
            }
            .foregroundStyle(Theme.Colors.tertiaryText)
            content()
                .font(Theme.Typography.body)
                .foregroundStyle(Theme.Colors.primaryText)
                .padding(.horizontal, Theme.Spacing.m)
                .padding(.vertical, Theme.Spacing.m)
                .background(Theme.Colors.surfaceSunken)
                .clipShape(RoundedRectangle(cornerRadius: Theme.Radius.s, style: .continuous))
                .overlay(
                    RoundedRectangle(cornerRadius: Theme.Radius.s, style: .continuous)
                        .strokeBorder(Theme.Colors.border, lineWidth: Theme.Stroke.hairline))
        }
    }

    private var assurance: some View {
        Text("Your password is stored only on this iPhone, in the Keychain. It never leaves the device "
             + "except to sign in to the Sali you named above.")
            .font(Theme.Typography.caption)
            .foregroundStyle(Theme.Colors.tertiaryText)
            .multilineTextAlignment(.center)
            .fixedSize(horizontal: false, vertical: true)
            .padding(.horizontal, Theme.Spacing.l)
    }

    // MARK: - Actions

    private var isSubmittable: Bool {
        Self.parseBaseURL(serverURL) != nil
            && !password.isEmpty
            && !deviceName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    @MainActor
    private func submit() async {
        guard isSubmittable, !isConnecting else { return }
        guard let url = Self.parseBaseURL(serverURL) else {
            failure = "That server address isn't valid — include http:// or https:// and a host."
            return
        }
        focusedField = nil
        failure = nil
        isConnecting = true
        defer { isConnecting = false }

        // Point the whole app at the typed URL BEFORE logging in, so the login request (and every request
        // after it) goes to the user's own server rather than any default.
        appState.applyConfiguration(APIConfiguration(environment: .production, baseURL: url))

        do {
            try await auth.login(
                password: password,
                deviceName: deviceName.trimmingCharacters(in: .whitespacesAndNewlines),
                model: Self.modelIdentifier())
            appState.didEnroll()
        } catch {
            failure = Self.describe(error)
        }
    }

    // MARK: - Helpers

    private static func parseBaseURL(_ raw: String) -> URL? {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty,
              let url = URL(string: trimmed),
              let scheme = url.scheme?.lowercased(), scheme == "http" || scheme == "https",
              let host = url.host, !host.isEmpty else { return nil }
        return url
    }

    private static func describe(_ error: Error) -> String {
        if let apiError = error as? APIError {
            switch apiError {
            case .offline:
                return "Couldn't reach that address. Check the server is running and the address is right "
                     + "(and that you're on the same network, or the tunnel is up)."
            case .forbidden:
                return "Incorrect password. Set or reset it on the machine with `sali change-password`."
            default:
                return apiError.errorDescription ?? "Sign-in failed. Try again."
            }
        }
        return "Sign-in failed. Try again."
    }

    private static func defaultDeviceName() -> String {
        #if canImport(UIKit)
        let name = UIDevice.current.name
        return name.isEmpty ? "iPhone" : name
        #else
        return "iPhone"
        #endif
    }

    private static func modelIdentifier() -> String? {
        #if canImport(UIKit)
        var info = utsname()
        uname(&info)
        let mirror = Mirror(reflecting: info.machine)
        let id = mirror.children.compactMap { ($0.value as? Int8).map { Character(UnicodeScalar(UInt8($0))) } }
            .filter { $0 != "\0" }
        return id.isEmpty ? nil : String(id)
        #else
        return nil
        #endif
    }
}
