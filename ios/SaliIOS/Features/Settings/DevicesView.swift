import Foundation
import SwiftUI

// Device management (§5/§24) — owner only. Pairing mints a short-lived, single-use code; revocation kills
// every session bound to a device immediately, even if that device is offline. Both are consequential, so
// revoke is gated behind a natural confirmation and the pairing code is shown once, prominently, with its
// expiry — never persisted in plaintext on this screen either.

@MainActor
final class DevicesViewModel: ObservableObject {
    @Published var state: Loadable<Void> = .idle
    @Published var devices: [DeviceInfo] = []

    @Published var isMintingCode = false
    @Published var mintError: String?
    @Published var mintedCode: EnrollCodeResult?

    @Published var workingDeviceId: String?
    @Published var revokeError: String?

    func load(api: APIClient) async {
        state = .loading
        do {
            let list: [DeviceInfo] = try await api.get("devices")
            devices = list.sorted { ($0.createdAt ?? .distantPast) > ($1.createdAt ?? .distantPast) }
            state = .loaded(())
        } catch {
            state = .failed((error as? APIError)?.errorDescription ?? "Couldn't load devices.")
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

    func revoke(_ device: DeviceInfo, api: APIClient) async {
        workingDeviceId = device.id
        revokeError = nil
        do {
            try await api.postVoid("devices/\(device.id)/revoke")
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
        .navigationTitle("Devices")
        .toolbar {
            if appState.role.canManageDevices {
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        showPairSheet = true
                    } label: {
                        Label("Pair a new device", systemImage: "plus")
                    }
                }
            }
        }
        .task {
            guard appState.role.canManageDevices else { return }
            if case .idle = viewModel.state { await viewModel.load(api: appState.api) }
        }
        .refreshable { await viewModel.load(api: appState.api) }
        .sheet(isPresented: $showPairSheet, onDismiss: { viewModel.mintedCode = nil }) {
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
            LoadingState("Loading devices…")
        case .failed(let message):
            ErrorStateView(message) { Task { await viewModel.load(api: appState.api) } }
        case .loaded:
            List {
                if viewModel.devices.isEmpty {
                    EmptyStateView(icon: "iphone", title: "No devices yet",
                                   message: "Pair an iPhone to start controlling Sali from it.")
                        .listRowInsets(EdgeInsets())
                } else {
                    Section {
                        ForEach(viewModel.devices) { device in
                            DeviceRow(device: device,
                                     isThisDevice: device.id == appState.tokenStore.deviceId,
                                     isWorking: viewModel.workingDeviceId == device.id) {
                                Task { await viewModel.revoke(device, api: appState.api) }
                            }
                        }
                    } footer: {
                        Text("Revoking a device ends every session bound to it immediately — even if that device is offline right now.")
                    }
                }
            }
            .listStyle(.insetGrouped)
        }
    }
}

// MARK: - Device row

private struct DeviceRow: View {
    let device: DeviceInfo
    let isThisDevice: Bool
    let isWorking: Bool
    let onRevoke: () -> Void

    @State private var showRevokeConfirmation = false

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: Theme.Spacing.xs) {
                        Text(device.name).font(Theme.Typography.body.weight(.medium))
                        if isThisDevice {
                            StatusPill("This device", color: Theme.Colors.accent)
                        }
                    }
                    if let model = device.model {
                        Text(model).font(Theme.Typography.caption).foregroundStyle(Theme.Colors.secondaryText)
                    }
                }
                Spacer()
                VStack(alignment: .trailing, spacing: Theme.Spacing.xs) {
                    StatusPill(device.role.capitalized, color: Theme.Colors.info)
                    StatusPill(device.isActive ? "Active" : "Revoked",
                              color: device.isActive ? Theme.Colors.ok : Theme.Colors.idle)
                }
            }

            HStack(spacing: Theme.Spacing.m) {
                if let lastSeen = device.lastSeenAt {
                    Label("seen \(lastSeen.saliRelative)", systemImage: "clock")
                } else {
                    Label("never connected", systemImage: "clock")
                }
                Label(device.hasPush ? "push on" : "push off",
                     systemImage: device.hasPush ? "bell.badge.fill" : "bell.slash")
            }
            .font(Theme.Typography.caption)
            .foregroundStyle(Theme.Colors.secondaryText)

            if device.isActive {
                if isWorking {
                    ProgressView().frame(maxWidth: .infinity, alignment: .center)
                } else {
                    Button(role: .destructive) {
                        showRevokeConfirmation = true
                    } label: {
                        Label("Revoke", systemImage: "xmark.shield")
                    }
                }
            }
        }
        .padding(.vertical, Theme.Spacing.xs)
        .accessibilityElement(children: .combine)
        .sheet(isPresented: $showRevokeConfirmation) {
            NaturalConfirmationSheet(
                title: "Revoke \(device.name)",
                explanation: isThisDevice
                    ? "This is the device you're using right now. Revoking it ends this session immediately — you'll be signed out and need a new pairing code to get back in."
                    : "This ends every session \(device.name) holds immediately, even while it's offline. It will need a brand-new pairing code to reconnect — this can't be undone from here.",
                confirmLabel: "Revoke this device",
                isDestructive: true
            ) {
                onRevoke()
            }
        }
    }
}

// MARK: - Pair a new device

private struct PairDeviceSheet: View {
    @ObservedObject var viewModel: DevicesViewModel
    let api: APIClient
    @Environment(\.dismiss) private var dismiss

    @State private var selectedRole: Role = .controller
    @State private var label: String = ""

    var body: some View {
        NavigationStack {
            Group {
                if let code = viewModel.mintedCode {
                    mintedCodeView(code)
                } else {
                    formView
                }
            }
            .navigationTitle("Pair a new device")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Close") { dismiss() }
                }
            }
        }
        .presentationDetents([.medium, .large])
    }

    private var formView: some View {
        Form {
            Section {
                Picker("Role", selection: $selectedRole) {
                    ForEach(Role.allCases, id: \.self) { role in
                        Text(role.label).tag(role)
                    }
                }
                TextField("Label (optional, e.g. \"Almir's iPhone\")", text: $label)
            } footer: {
                Text(roleExplanation)
            }

            if let error = viewModel.mintError {
                Text(error).font(Theme.Typography.caption).foregroundStyle(Theme.Colors.danger)
            }

            Section {
                Button {
                    Task { await viewModel.mintCode(role: selectedRole, label: label, api: api) }
                } label: {
                    if viewModel.isMintingCode {
                        ProgressView().frame(maxWidth: .infinity)
                    } else {
                        Text("Generate pairing code").frame(maxWidth: .infinity)
                    }
                }
                .buttonStyle(.borderedProminent)
                .tint(Theme.Colors.accent)
                .disabled(viewModel.isMintingCode)
            }
        }
    }

    private var roleExplanation: String {
        switch selectedRole {
        case .owner: "Full authority, including pairing and revoking other devices."
        case .controller: "Can send messages and direct Sali, but can't manage devices."
        case .observer: "Read-only — can watch what Sali is doing without directing it."
        }
    }

    private func mintedCodeView(_ code: EnrollCodeResult) -> some View {
        VStack(spacing: Theme.Spacing.l) {
            Image(systemName: "checkmark.seal.fill")
                .font(.system(size: 32))
                .foregroundStyle(Theme.Colors.ok)

            Text(code.code)
                .font(.system(.largeTitle, design: .monospaced).weight(.semibold))
                .kerning(1.5)
                .multilineTextAlignment(.center)
                .accessibilityLabel("Pairing code \(code.code.map(String.init).joined(separator: " "))")

            if let expiresAt = code.expiresAt {
                Text("Expires \(expiresAt.saliRelative)")
                    .font(Theme.Typography.caption)
                    .foregroundStyle(Theme.Colors.secondaryText)
            }

            Text("Single-use — enter this on the new device now. Once it's redeemed, or once it expires, this code stops working.")
                .font(Theme.Typography.caption)
                .foregroundStyle(Theme.Colors.secondaryText)
                .multilineTextAlignment(.center)

            Spacer(minLength: 0)

            Button("Done") { dismiss() }
                .buttonStyle(.borderedProminent)
                .tint(Theme.Colors.accent)
                .frame(maxWidth: .infinity)
        }
        .padding(Theme.Spacing.xl)
    }
}
