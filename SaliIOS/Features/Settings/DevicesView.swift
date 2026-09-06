import Foundation
import SwiftUI
#if canImport(UIKit)
import UIKit
#endif

// Device management (§5/§24) — owner only. Pairing mints a short-lived, single-use code; revocation kills
// every session bound to a device immediately, even if that device is offline. Both are consequential, so
// revoke is gated behind a natural confirmation and the pairing code is shown once, prominently, with a
// live countdown — never persisted in plaintext on this screen either.
//
// Three things this screen refuses to do:
//
// - **Blank on refresh.** Only a first load is a loading state; a pull-to-refresh, and the re-read that
//   follows a revoke, leave the list exactly where it is. A refresh that fails keeps the last-known list
//   and says so in one quiet line rather than replacing the screen with an error.
// - **Turn a row into a spinner.** Revoking keeps the row's shape and text; the action dims and reports
//   itself in place, so the list never jumps under the finger.
// - **Show a code you can't use.** A pairing code has to get to another device, so it is copyable, states
//   which role it grants, and counts down to its own expiry.

@MainActor
final class DevicesViewModel: ObservableObject {
    @Published var state: Loadable<Void> = .idle
    @Published var devices: [DeviceInfo] = []
    /// A refresh that failed while a list is already on screen. Never replaces the content — the last-known
    /// truth plus an honest note beats an empty screen.
    @Published var refreshFailure: String?

    @Published var isMintingCode = false
    @Published var mintError: String?
    @Published var mintedCode: EnrollCodeResult?

    @Published var workingDeviceId: String?
    @Published var revokeError: String?

    var active: [DeviceInfo] { devices.filter(\.isActive) }
    var revoked: [DeviceInfo] { devices.filter { !$0.isActive } }

    /// Only a first load is a loading state. `revoke` re-reads through here too, so the guard is what
    /// keeps revoking one device from blanking the whole list (items 2, 3).
    func load(api: APIClient) async {
        if case .loaded = state {} else { state = .loading }
        do {
            let list: [DeviceInfo] = try await api.get("devices")
            // Most recently seen first, then never-connected, then by pairing date — the order someone
            // actually scans a device list in.
            devices = list.sorted { lhs, rhs in
                switch (lhs.lastSeenAt, rhs.lastSeenAt) {
                case let (.some(l), .some(r)): return l > r
                case (.some, .none): return true
                case (.none, .some): return false
                case (.none, .none): return (lhs.createdAt ?? .distantPast) > (rhs.createdAt ?? .distantPast)
                }
            }
            refreshFailure = nil
            state = .loaded(())
        } catch {
            let message = (error as? APIError)?.errorDescription ?? "Couldn't load devices."
            if case .loaded = state {
                refreshFailure = message
            } else {
                state = .failed(message)
            }
        }
    }

    func mintCode(role: Role, label: String, api: APIClient) async {
        isMintingCode = true
        mintError = nil
        mintedCode = nil
        do {
            let trimmedLabel = label.trimmingCharacters(in: .whitespacesAndNewlines)
            var json: [String: Any] = ["role": role.rawValue]
            if !trimmedLabel.isEmpty { json["label"] = trimmedLabel }
            let result: EnrollCodeResult = try await api.post("enroll/code", json: json)
            mintedCode = result
        } catch {
            mintError = (error as? APIError)?.errorDescription ?? "Couldn't create a pairing code."
        }
        isMintingCode = false
    }

    /// Revoking *this* device ends the session that is making the request, so re-reading the list would
    /// just 401 its way to a forced sign-out. Signing out deliberately is the honest ending.
    func revoke(_ device: DeviceInfo, isThisDevice: Bool, api: APIClient,
                signOut: @escaping () -> Void) async {
        workingDeviceId = device.id
        revokeError = nil
        do {
            try await api.postVoid("devices/\(device.id)/revoke")
            if isThisDevice {
                workingDeviceId = nil
                signOut()
                return
            }
            await load(api: api)
        } catch {
            revokeError = (error as? APIError)?.errorDescription ?? "Couldn't revoke that device."
        }
        workingDeviceId = nil
    }
}

struct DevicesView: View {
    @EnvironmentObject var appState: AppState
    @StateObject private var viewModel = DevicesViewModel()
    @State private var showPairSheet = false

    var body: some View {
        Group {
            if !appState.role.canManageDevices {
                EmptyStateView(
                    icon: "lock.shield",
                    title: "Owner only",
                    message: "Device pairing and revocation stay under one authority so there's never ambiguity about who can grant or cut off access. Ask the owner's device to manage devices."
                )
            } else {
                content
            }
        }
        // The Theme canvas, not the system default. `saliList()` only paints behind a List — the empty,
        // error, and skeleton states are plain views, so without this they render on iOS's pure white and
        // the screen visibly changes colour the moment the content goes away.
        .background(Theme.Colors.background.ignoresSafeArea())
        .navigationTitle("Devices")
        .toolbar {
            if appState.role.canManageDevices {
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        showPairSheet = true
                    } label: {
                        Label("Pair a new device", systemImage: "plus")
                    }
                    .tint(Theme.Colors.accent)
                }
            }
        }
        .task {
            guard appState.role.canManageDevices else { return }
            if case .idle = viewModel.state { await viewModel.load(api: appState.api) }
        }
        .refreshable { await viewModel.load(api: appState.api) }
        .sheet(isPresented: $showPairSheet, onDismiss: {
            viewModel.mintedCode = nil
            viewModel.mintError = nil
        }) {
            PairDeviceSheet(viewModel: viewModel, api: appState.api)
        }
        .alert("Couldn't revoke that device", isPresented: Binding(
            get: { viewModel.revokeError != nil },
            set: { if !$0 { viewModel.revokeError = nil } }
        )) {
            Button("OK", role: .cancel) { viewModel.revokeError = nil }
        } message: {
            Text(viewModel.revokeError ?? "")
        }
    }

    @ViewBuilder private var content: some View {
        switch viewModel.state {
        case .idle, .loading:
            // Shaped like the rows that are coming: a title line, a trailing pill, two lines of metadata.
            SaliSkeletonList(rows: 3, lines: 2)
        case .failed(let message):
            ErrorStateView(message) { Task { await viewModel.load(api: appState.api) } }
        case .loaded:
            if viewModel.devices.isEmpty {
                EmptyStateView(icon: "iphone", title: "No devices yet",
                               message: "Pair an iPhone or iPad to give it a window into Sali. Codes are single-use and expire on their own.")
            } else {
                deviceList
            }
        }
    }

    private var deviceList: some View {
        List {
            if let refreshFailure = viewModel.refreshFailure {
                staleNotice(refreshFailure)
            }

            if !viewModel.active.isEmpty {
                Section {
                    ForEach(viewModel.active) { row(for: $0) }
                } header: {
                    SectionHeader("Paired devices",
                                  subtitle: "\(viewModel.active.count) can reach Sali right now.")
                } footer: {
                    SectionFooter("Revoking a device ends every session bound to it immediately — even if that device is offline right now.",
                                  detail: "\(viewModel.active.count) active")
                }
            }

            if !viewModel.revoked.isEmpty {
                Section {
                    ForEach(viewModel.revoked) { row(for: $0) }
                } header: {
                    SectionHeader("Revoked", emphasis: .secondary)
                } footer: {
                    SectionFooter("Kept as a record of what was once trusted. A revoked device needs a brand-new pairing code to come back.")
                }
            }
        }
        .listStyle(.insetGrouped)
        .saliList()
    }

    private func row(for device: DeviceInfo) -> some View {
        DeviceRow(device: device,
                  isThisDevice: device.id == appState.tokenStore.deviceId,
                  isWorking: viewModel.workingDeviceId == device.id) {
            Task {
                await viewModel.revoke(device,
                                       isThisDevice: device.id == appState.tokenStore.deviceId,
                                       api: appState.api,
                                       signOut: { appState.signOut() })
            }
        }
    }

    /// A refresh failed but the list below is still true as of the last successful read. Say that, keep the
    /// content, and offer the retry — never swap a loaded screen for an error page.
    private func staleNotice(_ message: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
            Image(systemName: "arrow.clockwise")
                .font(Theme.Typography.caption)
                .foregroundStyle(Theme.Colors.tertiaryText)
            Text("\(message) Showing the last list Sali sent.")
                .font(Theme.Typography.footnote)
                .foregroundStyle(Theme.Colors.secondaryText)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: Theme.Spacing.s)
            Button("Retry") { Task { await viewModel.load(api: appState.api) } }
                .font(Theme.Typography.footnote.weight(.semibold))
                .tint(Theme.Colors.accent)
                .frame(minHeight: 44)
        }
        .padding(.vertical, Theme.Spacing.xs)
        .listRowBackground(Theme.Colors.surfaceSunken)
        .accessibilityElement(children: .combine)
    }
}

// MARK: - Device row

/// One device, ranked: the name is the statement, everything the machine knows about it is metadata under
/// it, and only a state worth acting on earns a pill. An *active* device is the norm and gets no badge —
/// a list where every row wears two pills has no hierarchy at all.
private struct DeviceRow: View {
    let device: DeviceInfo
    let isThisDevice: Bool
    let isWorking: Bool
    let onRevoke: () -> Void

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var showRevokeConfirmation = false
    @ScaledMetric(relativeTo: .caption2) private var inlineMarkSize: CGFloat = 13

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
                Text(device.name)
                    .font(Theme.Typography.body.weight(.medium))
                    .foregroundStyle(Theme.Colors.primaryText)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: Theme.Spacing.s)
                if isThisDevice {
                    StatusPill("This device", color: Theme.Colors.accent)
                } else if !device.isActive {
                    StatusPill("Revoked", color: Theme.Colors.idle)
                }
            }

            Text(identityLine)
                .font(Theme.Typography.metadata)
                .foregroundStyle(Theme.Colors.tertiaryText)

            Text(activityLine)
                .font(Theme.Typography.metadata)
                .foregroundStyle(Theme.Colors.tertiaryText)

            if device.isActive {
                revokeControl
            }
        }
        .padding(.vertical, Theme.Spacing.xs)
        .opacity(device.isActive ? 1 : 0.65)
        .swipeActions(edge: .trailing) {
            if device.isActive {
                Button(role: .destructive) { showRevokeConfirmation = true } label: {
                    Label("Revoke", systemImage: "xmark.shield")
                }
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(device.name). \(identityLine). \(activityLine).")
        .sheet(isPresented: $showRevokeConfirmation) {
            NaturalConfirmationSheet(
                title: "Revoke \(device.name)",
                explanation: isThisDevice
                    ? "This is the device you're using right now. Revoking it ends this session immediately — you'll be signed out here and will need a new pairing code to get back in."
                    : "This ends every session \(device.name) holds immediately, even while it's offline. It will need a brand-new pairing code to reconnect — this can't be undone from here.",
                confirmLabel: "Revoke this device",
                isDestructive: true
            ) {
                onRevoke()
            }
        }
    }

    /// The action keeps its shape while it runs — same label, same position, dimmed and inert with the mark
    /// alongside it. Swapping it for a centred spinner made the row jump and the list reflow mid-tap.
    private var revokeControl: some View {
        Button(role: .destructive) {
            showRevokeConfirmation = true
        } label: {
            HStack(spacing: Theme.Spacing.s) {
                if isWorking {
                    SaliMark(size: inlineMarkSize, color: Theme.Colors.secondaryText, animated: !reduceMotion)
                } else {
                    Image(systemName: "xmark.shield")
                        .font(Theme.Typography.footnote)
                }
                Text(isWorking ? "Revoking…" : "Revoke")
                    .font(Theme.Typography.footnote.weight(.medium))
            }
            // Ink at rest. A screen listing twenty-five devices was twenty-five red labels, which is not
            // signal — it's decoration in the one colour the system reserves for consequences. The
            // consequence is stated in danger where it belongs: on the confirmation sheet.
            .foregroundStyle(Theme.Colors.secondaryText)
            .opacity(isWorking ? 0.6 : 1)
            .frame(minHeight: 44, alignment: .leading)
            .frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(isWorking)
        .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.quick), value: isWorking)
        .accessibilityLabel(isWorking ? "Revoking \(device.name)" : "Revoke \(device.name)")
    }

    /// What the device *is* — role first, because it is the only part that grants authority.
    private var identityLine: String {
        var parts = [device.role.capitalized]
        if let model = device.model, !model.isEmpty { parts.append(model) }
        else if !device.platform.isEmpty { parts.append(device.platform) }
        return parts.joined(separator: "  ·  ")
    }

    /// What the device has *done* — when Sali last heard from it, and whether push is registered.
    private var activityLine: String {
        var parts: [String] = []
        if device.isActive {
            parts.append(device.lastSeenAt.map { "Seen \(Self.recency($0))" } ?? "Never connected")
        } else {
            parts.append(device.revokedAt.map { "Revoked \(Self.recency($0))" } ?? "Revoked")
        }
        if let created = device.createdAt { parts.append("paired \(Self.recency(created))") }
        // Only when push is actually registered. "no push token" on every row said nothing about the
        // device and everything about a capability the app doesn't have yet — Settings states that once.
        if device.hasPush { parts.append("push registered") }
        return parts.joined(separator: "  ·  ")
    }

    /// `saliRelative` renders the last-seen stamp of the device you are holding as "in 0 sec" — the host
    /// clock is a hair ahead, and `RelativeDateTimeFormatter` reports the future literally. Anything inside
    /// a minute, in either direction, is simply now.
    private static func recency(_ date: Date) -> String {
        abs(date.timeIntervalSinceNow) < 60 ? "just now" : date.saliRelative
    }
}

// MARK: - Pair a new device

private struct PairDeviceSheet: View {
    @ObservedObject var viewModel: DevicesViewModel
    let api: APIClient
    @Environment(\.dismiss) private var dismiss
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    @State private var selectedRole: Role = .controller
    @State private var label: String = ""
    @State private var didCopy = false
    @FocusState private var labelFocused: Bool

    @ScaledMetric(relativeTo: .largeTitle) private var markSize: CGFloat = 34
    @ScaledMetric(relativeTo: .body) private var buttonMarkSize: CGFloat = 17

    var body: some View {
        NavigationStack {
            Group {
                if let code = viewModel.mintedCode {
                    mintedCodeView(code)
                } else {
                    formView
                }
            }
            .navigationTitle(viewModel.mintedCode == nil ? "Pair a new device" : "Pairing code")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button(viewModel.mintedCode == nil ? "Cancel" : "Close") { dismiss() }
                        .tint(Theme.Colors.accent)
                }
            }
        }
        .presentationDetents([.medium, .large])
    }

    // MARK: Form

    private var formView: some View {
        Form {
            Section {
                Picker("Role", selection: $selectedRole) {
                    ForEach(Role.allCases, id: \.self) { role in
                        Text(role.label).tag(role)
                    }
                }
                .frame(minHeight: 44)

                VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                    Text("Label")
                        .font(Theme.Typography.subheading)
                        .foregroundStyle(Theme.Colors.secondaryText)
                    TextField("Optional — e.g. \"Almir's iPhone\"", text: $label)
                        .font(Theme.Typography.body)
                        .foregroundStyle(Theme.Colors.primaryText)
                        .focused($labelFocused)
                        .modifier(PairFieldWell(isFocused: labelFocused, reduceMotion: reduceMotion))
                        .accessibilityLabel("Device label")
                }
                .padding(.vertical, Theme.Spacing.xs)
            } header: {
                SectionHeader("Who this device will be",
                              subtitle: "The role is baked into the code — it can't be changed at the other end.")
            } footer: {
                SectionFooter(roleExplanation)
            }
            // Explicit, because iOS resolves a grouped row inside a *presented* sheet to a light-mode grey
            // that is DARKER than the canvas behind it — so the card read as a dent and the input well
            // inside it read as raised, exactly inverting the elevation ramp. Naming the step fixes both
            // schemes deterministically.
            .listRowBackground(Theme.Colors.surfaceRaised)

            Section {
                Button {
                    Task { await viewModel.mintCode(role: selectedRole, label: label, api: api) }
                } label: {
                    HStack(spacing: Theme.Spacing.s) {
                        if viewModel.isMintingCode {
                            SaliMark(size: buttonMarkSize, color: Theme.Colors.onAccent, animated: !reduceMotion)
                        }
                        Text(viewModel.isMintingCode ? "Asking Sali…" : "Generate pairing code")
                    }
                    .frame(maxWidth: .infinity)
                    .frame(minHeight: 28)
                }
                .buttonStyle(SaliPrimaryButtonStyle())
                .disabled(viewModel.isMintingCode)

                if let error = viewModel.mintError {
                    Label {
                        Text(error).font(Theme.Typography.footnote)
                    } icon: {
                        Image(systemName: "exclamationmark.triangle.fill").font(Theme.Typography.caption)
                    }
                    .foregroundStyle(Theme.Colors.danger)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.top, Theme.Spacing.s)
                    .accessibilityElement(children: .combine)
                }
            }
            .listRowBackground(Color.clear)
            .listRowInsets(EdgeInsets(top: Theme.Spacing.s, leading: Theme.Spacing.listMargin,
                                      bottom: Theme.Spacing.s, trailing: Theme.Spacing.listMargin))
        }
        .listStyle(.insetGrouped)
        .saliList()
        .animation(Theme.Motion.honoring(reduceMotion, Theme.Motion.quick), value: viewModel.isMintingCode)
    }

    private var roleExplanation: String {
        switch selectedRole {
        case .owner: "Full authority, including pairing and revoking other devices. Give this out sparingly."
        case .controller: "Can send messages and direct Sali, but can't manage devices."
        case .observer: "Read-only — can watch what Sali is doing without directing it."
        }
    }

    // MARK: The code

    private func mintedCodeView(_ code: EnrollCodeResult) -> some View {
        ScrollView {
            VStack(spacing: Theme.Spacing.l) {
                SaliMark(size: markSize, color: Theme.Colors.accent, animated: !reduceMotion)
                    .padding(.top, Theme.Spacing.l)

                Text(code.code)
                    .font(Theme.Typography.display.monospaced())
                    .foregroundStyle(Theme.Colors.primaryText)
                    .kerning(2)
                    .multilineTextAlignment(.center)
                    .minimumScaleFactor(0.6)
                    .lineLimit(1)
                    .accessibilityLabel("Pairing code \(code.code.map(String.init).joined(separator: " "))")

                expiry(code)

                copyButton(code)

                Text("Enter this on the new device's pairing screen. It grants **\(code.role.capitalized)** access, works exactly once, and stops working the moment it is used or expires.")
                    .font(Theme.Typography.footnote)
                    .foregroundStyle(Theme.Colors.secondaryText)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)

                Spacer(minLength: Theme.Spacing.l)

                Button { dismiss() } label: { Text("Done").frame(maxWidth: .infinity) }
                    .buttonStyle(SaliPrimaryButtonStyle())
            }
            .padding(Theme.Spacing.xl)
            .frame(maxWidth: .infinity)
        }
        .background(Theme.Colors.background)
    }

    /// A live countdown, not a stamp read once when the sheet opened. A ten-minute code that says
    /// "expires in 10 minutes" forever is worse than no expiry at all.
    @ViewBuilder private func expiry(_ code: EnrollCodeResult) -> some View {
        if let expiresAt = code.expiresAt {
            HStack(spacing: Theme.Spacing.xs) {
                Text("Expires in")
                Text(expiresAt, style: .relative).monospacedDigit()
            }
            .font(Theme.Typography.metadata)
            .foregroundStyle(Theme.Colors.tertiaryText)
            .accessibilityElement(children: .combine)
        } else {
            Text("Single-use, and short-lived.")
                .font(Theme.Typography.metadata)
                .foregroundStyle(Theme.Colors.tertiaryText)
        }
    }

    /// A code that has to reach another device is useless if it can only be read off the glass.
    private func copyButton(_ code: EnrollCodeResult) -> some View {
        Button {
            #if canImport(UIKit)
            UIPasteboard.general.string = code.code
            #endif
            withAnimation(Theme.Motion.honoring(reduceMotion, Theme.Motion.quick)) { didCopy = true }
            Task {
                try? await Task.sleep(nanoseconds: UInt64(Theme.Motion.Duration.beat * 2 * 1_000_000_000))
                withAnimation(Theme.Motion.honoring(reduceMotion, Theme.Motion.gentle)) { didCopy = false }
            }
        } label: {
            Label(didCopy ? "Copied" : "Copy code",
                  systemImage: didCopy ? "checkmark" : "doc.on.doc")
                .font(Theme.Typography.footnote.weight(.semibold))
                .frame(minHeight: 44)
                .padding(.horizontal, Theme.Spacing.l)
                .contentShape(Rectangle())
        }
        .buttonStyle(.bordered)
        .tint(Theme.Colors.accent)
        .accessibilityLabel(didCopy ? "Pairing code copied" : "Copy pairing code")
    }
}

// MARK: - The input well

/// Text fields belong on `surfaceSunken`, edged, and emphasised on focus — see `Theme.Colors`. A bare
/// `TextField` in a grouped row is the same colour as the row and reads as static text.
private struct PairFieldWell: ViewModifier {
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
